"""來源健康度：這個名錄還在正常地被抓回來嗎。

整套爬取建立在 CSS 選擇器上。網站一改版，選擇器就指到別的地方——而症狀**不是
報錯**：爬取會「成功」地抓到 0 筆、或者抓回 12 筆（上一次 380 筆），畫面上寫著
完成。排程是半夜自己跑的，沒有人在看。

每一次的筆數本來就都存在 ``crawl_jobs`` 裡了，缺的只是有人拿它們來比。

## 為什麼基準是中位數，不是「上一次」

這裡本來比的是上一次，而那有一個洞：**警告只會出現一次**。禮拜一壞掉抓回 12
筆（跳警告），禮拜二又是 12 筆——跟上一次比完全正常，於是不再警告。偏偏禮拜二
這個來源已經壞了一整天。壞掉的第二天警告自己消失，是最糟的一種行為。

拿最近幾次「真的抓到東西」的執行取中位數就沒有這個問題：一次壞掉拉不動中位數。
而如果一個名錄是真的變小了（會員退掉一半），連續幾次都是新的數字之後中位數自己
會跟上來，警告就停了——不必請使用者去按「不要再提醒我」。

## 這裡只做判斷，不碰資料庫

要比的數字從哪裡來是呼叫的人的事（爬完那一刻是手上的 :class:`CrawlSummary`，
「爬取」頁上是 ``crawl_jobs`` 裡存的紀錄）。判斷只有一份，兩邊共用——兩份遲早
會長得不一樣，而不一樣的那一天沒有人會發現。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from statistics import median

from core.constants import CrawlStatus
from core.schemas import CrawlSummary

#: 拿最近幾次「真的抓到東西」的執行來算基準。
#:
#: 5 次夠讓一兩次的異常淹掉，又不至於久到一個真的縮水了的名錄要等半年才被
#: 接受。再多的意義不大——名錄改版的頻率遠低於這個。
BASELINE_RUNS = 5

#: 抓到的筆數掉到基準的這個比例以下，就當成「這個網站可能改版了」。
#:
#: 0.4 是刻意寬鬆的。名錄本來就會增減，有些站每個月換一批廠商；抓到六成
#: 還在正常範圍。真正要攔的是 0 筆、以及「380 筆變 12 筆」那種斷崖。
DROP_RATIO = 0.4


class HealthState(str, Enum):
    """一個來源現在的狀態。值只在程式內部用，畫面上顯示的是 :data:`STATE_LABELS`。"""

    OK = "ok"
    #: 從來沒爬過。
    NEVER = "never"
    #: 爬過，但還沒有可以拿來比的執行。
    NO_BASELINE = "no_baseline"
    #: 上一次沒跑到最後（正在跑、或是程式被關掉了）。
    UNFINISHED = "unfinished"
    #: 上一次是使用者自己停掉的。
    CANCELLED = "cancelled"
    #: 上一次是接續前一次沒跑完的部分，筆數本來就只有一截。
    RESUMED = "resumed"
    #: 上一次失敗了。
    FAILED = "failed"
    #: 抓到了東西，但沒有一筆是公司資料。
    REJECTED = "rejected"
    #: 這次一筆都沒抓到。
    EMPTY = "empty"
    #: 掉得比平常少太多。
    DROPPED = "dropped"
    #: 這個來源沒有啟用，排程不會去爬它。
    DISABLED = "disabled"


STATE_LABELS: dict[HealthState, str] = {
    HealthState.OK: "正常",
    HealthState.NEVER: "還沒爬過",
    HealthState.NO_BASELINE: "還沒有基準",
    HealthState.UNFINISHED: "上次沒跑完",
    HealthState.CANCELLED: "上次被停掉",
    HealthState.RESUMED: "上次是接續跑",
    HealthState.FAILED: "上次失敗",
    HealthState.REJECTED: "抓到的不是公司",
    HealthState.EMPTY: "一筆都沒抓到",
    HealthState.DROPPED: "抓到的變少了",
    HealthState.DISABLED: "沒有啟用",
}

#: 需要使用者去看一眼的狀態。
#:
#: 「上次被停掉」與「上次是接續跑」不在裡面：那兩個是使用者自己造成的，而且
#: 下一次跑完就會回到正常。把它們算成警告，等於每次暫停都留下一個紅字。
NEEDS_ATTENTION = frozenset(
    {
        HealthState.FAILED,
        HealthState.REJECTED,
        HealthState.EMPTY,
        HealthState.DROPPED,
    }
)


@dataclass(frozen=True, slots=True)
class Baseline:
    """這個來源「平常」會抓到幾筆。"""

    counts: tuple[int, ...] = ()

    @property
    def runs(self) -> int:
        return len(self.counts)

    @property
    def value(self) -> int | None:
        """中位數，沒有東西可以比就是 ``None``。"""
        if not self.counts:
            return None
        return int(median(self.counts))

    def describe(self) -> str:
        """給人看的一句「平常是多少」。

        只有一次可以比的時候講「上一次」，而不是硬要說成「平常」——使用者
        看得出來程式只有一筆資料，講得太篤定反而讓人不信任後面那句建議。
        """
        value = self.value
        if value is None:
            return "以前沒有抓到過"
        if self.runs == 1:
            return f"上一次有 {value} 筆"
        return f"前 {self.runs} 次平常有 {value} 筆"


@dataclass(frozen=True, slots=True)
class SourceHealth:
    """一個來源的健康度判斷結果。"""

    source: str
    state: HealthState
    latest: CrawlSummary | None = None
    baseline: Baseline = field(default_factory=Baseline)
    enabled: bool = True
    #: 需要對使用者說的那一句。沒問題就是 ``None``——爬取管線直接把它塞進
    #: :attr:`CrawlSummary.warning`，所以這裡有值就等於畫面上會跳提醒。
    warning: str | None = None

    @property
    def needs_attention(self) -> bool:
        return self.state in NEEDS_ATTENTION

    @property
    def label(self) -> str:
        return STATE_LABELS.get(self.state, self.state.value)

    @property
    def found(self) -> int | None:
        return None if self.latest is None else self.latest.records_found

    @property
    def last_run_at(self) -> datetime | None:
        if self.latest is None:
            return None
        return self.latest.finished_at or self.latest.started_at

    @property
    def note(self) -> str:
        """「爬取」頁那一欄要寫的字。

        失敗的執行**不會**有 :attr:`warning`（理由見 :func:`assess`），但畫面
        上總得寫點什麼——那時候要寫的就是它自己的錯誤訊息。
        """
        if self.warning:
            return self.warning
        if self.state is HealthState.FAILED and self.latest is not None:
            return self.latest.error or ""
        return ""


def assess(
    source: str,
    latest: CrawlSummary | None,
    baseline: Baseline | None = None,
    *,
    enabled: bool = True,
) -> SourceHealth:
    """判斷一個來源現在的狀態。

    ``latest`` 是最近一次的執行（``None`` 代表從來沒爬過），``baseline`` 是
    **不含這一次**的前幾次筆數。
    """
    baseline = baseline or Baseline()

    def result(state: HealthState, warning: str | None = None) -> SourceHealth:
        return SourceHealth(
            source=source,
            state=state,
            latest=latest,
            baseline=baseline,
            enabled=enabled,
            warning=warning,
        )

    if latest is None:
        return result(HealthState.NEVER)

    # 沒有啟用的來源不判斷健康度：排程不會去爬它，那些數字只是舊資料。留一個
    # 紅字在那裡，使用者唯一能做的事是把它重新啟用——那不是提醒，是嘮叨。
    if not enabled:
        return result(HealthState.DISABLED)

    if latest.status not in (CrawlStatus.SUCCESS.value, CrawlStatus.PARTIAL.value):
        # 失敗與取消本來就有自己的訊息，再加一句「抓得比上次少」只是雜訊。
        # 狀態仍然標出來——半夜排程失敗的話，這是隔天早上唯一看得到的線索。
        if latest.status == CrawlStatus.FAILED.value:
            return result(HealthState.FAILED)
        if latest.status == CrawlStatus.CANCELLED.value:
            return result(HealthState.CANCELLED)
        return result(HealthState.UNFINISHED)

    # 抓到了東西、卻一筆都沒存進去。這跟「抓不到」是完全不同的毛病：頁面讀到
    # 了、重複區塊也找對了，是「公司名稱」那一欄指到了別的位置（子分類代號、
    # 表頭、導覽列）。以前這種情形畫面上只寫「找到 21、新增 0、拒絕 21」，
    # 三個數字擺在一起沒有人看得出那是壞的——這一句要直接講出來。
    #
    # 放在「跟平常比」的前面：第一次跑就抓錯的話根本沒有基準可以比，而那正是
    # 最需要被提醒的時候。
    if (
        latest.records_found
        and latest.records_new == 0
        and latest.records_updated == 0
        and latest.records_invalid >= latest.records_found
    ):
        return result(
            HealthState.REJECTED,
            f"抓到 {latest.records_found} 筆，但全部都不是公司資料，一筆都沒有存進去。"
            "通常是「公司名稱」抓到了別的位置——到日誌頁看它實際抓到什麼文字，"
            "再重新分析一次這個來源。",
        )

    # 接續上一次的執行本來就只做剩下的部分，筆數少是正常的。
    if latest.resumed:
        return result(HealthState.RESUMED)

    before = baseline.value
    if not before:
        return result(HealthState.NO_BASELINE)

    if latest.records_found == 0:
        return result(
            HealthState.EMPTY,
            f"這次一筆都沒抓到，{baseline.describe()}。"
            "這個網站很可能改版了，請重新分析一次這個來源。",
        )
    if latest.records_found < before * DROP_RATIO:
        return result(
            HealthState.DROPPED,
            f"這次只抓到 {latest.records_found} 筆，{baseline.describe()}。"
            "掉這麼多通常代表網站的版面變了，建議重新分析確認一次。",
        )
    return result(HealthState.OK)


def summarise(reports: list[SourceHealth]) -> str:
    """整批的一句話，給「爬取」頁的標題列用。"""
    if not reports:
        return "還沒有任何來源。"
    bad = [r for r in reports if r.needs_attention]
    if bad:
        names = "、".join(r.source for r in bad[:3])
        more = f" 等 {len(bad)} 個" if len(bad) > 3 else ""
        return f"{names}{more} 需要看一下。"
    unknown = sum(1 for r in reports if r.state in (HealthState.NEVER, HealthState.NO_BASELINE))
    if unknown == len(reports):
        return "還沒有足夠的紀錄可以比對——多爬幾次就會開始提醒你。"
    return f"{len(reports)} 個來源都正常。"
