"""AI 相關畫面與 :mod:`ai` 之間的那一層。

跟其他 controller 一樣：畫面不直接 import :mod:`ai.provider`，也不自己組
訊息串。這裡負責把「一段對話」翻成供應商要的東西，並且**保證 system prompt
一定在最前面**——那件事不能交給畫面去記得做。

抽取那條路（:meth:`AIController.extract_url`）多守一件事：**網頁一律由
:mod:`crawler.fetcher` 抓**，而不是由模型。那一層才有 robots.txt 檢查與請求
間隔，理由見 :mod:`ai.prompts`。這裡把兩邊接起來，順序永遠是「先抓，再給模型
讀」——沒有任何參數可以顛倒它。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

from ai.extract import AI_SOURCE, ExtractResult
from ai.prompts import build_system_prompt
from ai.provider import (
    ChatMessage,
    forget_probes,
    Model,
    ProviderStatus,
    available_providers,
    configured_providers,
    get_provider,
    provider_status,
)
from core.config import AppConfig, get_config, save_user_settings
from core.errors import AIError
from core.schemas import RawCompany

#: 一次帶幾則歷史訊息給模型。
#:
#: 不是無上限往回帶：每一則都要付 token（OpenRouter 是錢，本機是時間），而且
#: 超過模型的上下文長度時對方會直接回錯誤，而不是自己截斷。20 則大約是十輪
#: 來回，聊天情境下夠用；真的需要更長的脈絡是另一個功能，不是把這個數字調大。
MAX_HISTORY_MESSAGES = 20

#: 串流回覆時每收到幾個字回報一次進度。
#:
#: 不是每一段都報：串流一段常常只有兩三個字，一頁名錄會產生上萬段，而每一段
#: 都是一次跨執行緒的 signal 加一次畫面更新。實測那樣會讓抽取進行中的視窗
#: 明顯變鈍——回報是為了讓使用者知道還活著，不是為了精確。
_REPORT_EVERY_CHARS = 500


class ExtractCancelled(AIError):
    """使用者自己按了取消。

    要一個專屬的例外，是為了讓畫面分得出「壞掉了」與「他自己停的」——後者不
    該跳錯誤視窗，那會讓人以為按取消把東西弄壞了。
    """


def normalize_url(url: str) -> str:
    """把使用者貼進來的東西變成一個抓得動的網址。

    沒寫通訊協定時補上 ``https://``：從網址列複製常常只複製到
    ``example.com.tw/members``，為了這件事把人擋下來很沒有必要。

    但只在那一段**看起來像主機名稱**時才補。無條件補的話「這不是網址」也會
    變成一個合法的 URL，然後使用者拿到的是一個 DNS 查不到的錯誤訊息，而不是
    「你打的不是網址」。有寫通訊協定時就不做這個判斷——那時候他很清楚自己在
    打什麼，內網主機沒有點也是正常的。
    """
    text = (url or "").strip()
    if not text:
        raise AIError("先貼一個網址進來。")
    if "://" not in text:
        host = text.split("/", 1)[0]
        if " " in text or "." not in host:
            raise AIError(
                f"「{text}」不像一個網址。要的是 https://example.com.tw/… 這種東西。"
            )
        text = f"https://{text}"
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise AIError(
            f"「{url.strip()}」不像一個網址。要的是 https://example.com.tw/… 這種東西。"
        )
    return text


def _tick(report: Callable[[object], None] | None, message: str) -> None:
    if report is not None:
        report(message)


def _stop_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ExtractCancelled("已取消。")


@dataclass(frozen=True, slots=True)
class ChatTurn:
    """畫面保存的一則對話。與 :class:`~ai.provider.ChatMessage` 分開是因為
    畫面還要記「這則是不是還在串流中」之類的東西。"""

    role: str
    content: str


@dataclass(frozen=True, slots=True)
class AIStatus:
    """畫面要顯示的一切，一次算好。

    分開成一個快照物件，是因為「現在可以用嗎」這件事對 Ollama 來說要真的連一
    次線。畫面上有三個地方要知道答案（送出鈕、目前用哪一個、要不要顯示隱私
    警告），各問各的就是三次連線——實測讓一次頁面刷新卡了 7 秒。
    """

    ready: bool
    provider_label: str
    sends_data_off_device: bool
    #: 每個供應商的逐項狀態，設定頁用。
    providers: tuple[ProviderStatus, ...] = ()


@dataclass(frozen=True, slots=True)
class SaveResult:
    """把抽出來的資料存進名單之後的結果。"""

    new: int = 0
    #: 併進一筆已經存在的公司（補了它缺的欄位）。
    merged: int = 0
    #: 這一批自己內部的重複，連 upsert 都沒送。
    duplicate: int = 0
    #: 清理階段判定不是公司資料而丟掉的。
    rejected: int = 0

    def describe(self) -> str:
        parts = [f"新增 {self.new} 筆"]
        if self.merged:
            parts.append(f"併進既有的 {self.merged} 筆")
        if self.duplicate:
            parts.append(f"這批裡面自己重複 {self.duplicate} 筆")
        if self.rejected:
            parts.append(f"{self.rejected} 筆不像公司資料被丟掉")
        return "，".join(parts)


class AIController:
    """聊天與模型清單。"""

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()

    # ------------------------------------------------------------- 設定狀態

    @staticmethod
    def provider_options() -> dict[str, str]:
        return available_providers()

    def statuses(self) -> list[ProviderStatus]:
        return provider_status(self.config)

    def status(
        self,
        *,
        report: Callable[[object], None] | None = None,
        cancel_event: object | None = None,
    ) -> AIStatus:
        """一次算完畫面要的所有狀態，只探測一次。

        **會連網**（Ollama 的探測），所以呼叫端要放在
        :class:`~gui_qt.tasks.BackgroundTask` 裡。``report``／``cancel_event``
        是為了符合那個介面而收下的，這裡用不到。
        """
        ready = bool(configured_providers(self.config))
        if not ready:
            return AIStatus(
                ready=False,
                provider_label="",
                sends_data_off_device=True,
                providers=tuple(provider_status(self.config)),
            )
        provider = get_provider(self.config.ai.provider, self.config)
        return AIStatus(
            ready=True,
            provider_label=provider.label,
            sends_data_off_device=provider.sends_data_off_device,
            providers=tuple(provider_status(self.config)),
        )

    @staticmethod
    def forget_probes() -> None:
        """使用者改了設定，下一次重新探測而不要吃快取。"""
        forget_probes()

    def ready(self) -> bool:
        """有沒有任何一個供應商現在就能用。

        單獨問這一件事時用它。畫面要同時知道好幾件事的話用 :meth:`status`，
        那樣只會探測一次。
        """
        return bool(configured_providers(self.config))

    def active_provider_label(self) -> str:
        """現在實際會用哪一個，講人話。設定頁與聊天頁都要顯示這個。"""
        try:
            provider = get_provider(self.config.ai.provider, self.config)
        except AIError as exc:
            return str(exc).splitlines()[0]
        return provider.label

    def sends_data_off_device(self) -> bool:
        """現在選的供應商會不會把內容送出這台機器。

        畫面要靠這個決定要不要顯示隱私提醒。查不出來時回 ``True``——不確定的
        時候要往「會外送」猜，猜錯的代價是多顯示一行提醒，反過來猜錯的代價是
        使用者以為資料留在本機。
        """
        try:
            return get_provider(self.config.ai.provider, self.config).sends_data_off_device
        except AIError:
            return True

    # ------------------------------------------------------------- 模型清單

    def models(
        self,
        provider_name: str | None = None,
        *,
        report: Callable[[object], None] | None = None,
        cancel_event: object | None = None,
    ) -> list[Model]:
        """跟供應商要一份可用模型清單。會連網。

        ``report``／``cancel_event`` 收下就丟掉：這個方法會被
        :class:`~gui_qt.tasks.BackgroundTask` 當成 worker 呼叫，而它一律用
        關鍵字傳這兩個參數進來（見 gui_qt/tasks.py 的說明）。這裡沒有進度可
        回報、也沒有中途可取消的點，但簽名必須收得下，否則畫面一按就
        TypeError。
        """
        provider = get_provider(provider_name or self.config.ai.provider, self.config)
        return provider.list_models()

    def remember_choice(self, provider_name: str, model_id: str) -> None:
        """把使用者選的供應商與模型存進 ``user_settings.yaml``。

        存在那裡而不是 ``config.yaml``：後者會進 git，而且更新程式時會被
        `git pull` 碰到。使用者的選擇是他自己的東西，不該兩邊打架。
        """
        save_user_settings("ai", {"provider": provider_name, "model": model_id})
        self.config = get_config()

    def remember_prompt(self, prompt: str) -> None:
        save_user_settings("ai", {"system_prompt": prompt})
        self.config = get_config()

    # ------------------------------------------------------------------ 聊天

    def system_prompt(self) -> str:
        """實際會送出去的那一段，含使用者的補充指示。"""
        return build_system_prompt(self.config.ai.system_prompt)

    def chat(
        self,
        history: Sequence[ChatTurn],
        *,
        model: str | None = None,
        provider_name: str | None = None,
        on_chunk: Callable[[str], None] | None = None,
    ) -> str:
        """送一輪對話出去。

        ``history`` 是畫面上看得到的東西，不含 system prompt——那一段由這裡
        補上，畫面沒有辦法把它拿掉或改掉。
        """
        provider = get_provider(provider_name or self.config.ai.provider, self.config)
        chosen = (model or self.config.ai.model or "").strip()
        if not chosen:
            raise AIError("還沒有選模型。到「設定」頁的「AI 模型」選一個。")

        recent = list(history)[-MAX_HISTORY_MESSAGES:]
        messages = [ChatMessage("system", self.system_prompt())]
        messages.extend(ChatMessage(turn.role, turn.content) for turn in recent)
        return provider.chat(messages, chosen, on_chunk=on_chunk)

    # ------------------------------------------------------------ 從網址抽取

    def extract_url(
        self,
        url: str,
        *,
        model: str | None = None,
        provider_name: str | None = None,
        report: Callable[[object], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ExtractResult:
        """抓一頁、請模型讀完、把對得回原文的公司交出來。**不寫資料庫。**

        分成「抽取」與「存檔」兩步是刻意的：中間那一步是使用者看預覽表格、
        把不要的勾掉。少了那一步，一個把整頁導覽選單當成公司名稱的模型可以
        在三秒內把兩百筆垃圾灌進名單裡，而清掉它們要花的時間遠比看一眼多。

        抓網頁的是 :mod:`crawler.fetcher`，不是模型——robots.txt 被擋下來的話
        這個方法在**送出任何東西給模型之前**就會丟
        :class:`~core.errors.RobotsDisallowedError`。
        """
        from ai.extract import EXTRACT_MAX_TOKENS, extract_from_html
        from crawler.fetcher import build_fetcher, decode_bytes
        from crawler.parser import sniff_declared_encoding

        target = normalize_url(url)
        provider = get_provider(provider_name or self.config.ai.provider, self.config)
        chosen = (model or self.config.ai.model or "").strip()
        if not chosen:
            raise AIError("還沒有選模型。到「設定」頁的「AI 模型」選一個。")

        _tick(report, f"正在抓 {target} …")
        _stop_if_cancelled(cancel_event)
        with build_fetcher(self.config) as fetcher:
            page = fetcher.fetch(target)
        if not page.ok:
            raise AIError(f"抓不到那一頁：對方回了 HTTP {page.status_code}。")

        # 頁面自己宣告的編碼優先於 HTTP 標頭。台灣不少公協會名錄是 Big5 的
        # 舊站，標頭只寫 text/html 不附 charset，這時整頁中文會變成亂碼——而
        # 亂碼的公司名稱看起來仍然「有值」，只會安靜地存進一堆看不懂的字。
        html = page.html
        declared = sniff_declared_encoding(page.raw) if page.raw else None
        if declared:
            html = decode_bytes(page.raw, declared)

        _stop_if_cancelled(cancel_event)
        _tick(report, "抓到了，正在請模型讀這一頁…")

        def chat(messages: Sequence[ChatMessage]) -> str:
            received = 0
            reported = 0

            def on_chunk(piece: str) -> None:
                nonlocal received, reported
                # 串流的每一段都是一次「還活著」的證明，也是**唯一**能中途
                # 停下來的地方：模型回一頁名錄可能要好幾分鐘，等它整段回完
                # 才看 cancel_event 的話，取消鈕按下去到真的停要等一樣久。
                _stop_if_cancelled(cancel_event)
                received += len(piece)
                if received - reported >= _REPORT_EVERY_CHARS:
                    reported = received
                    _tick(report, f"模型正在回覆…（已收到 {received:,} 字）")

            return provider.chat(
                messages, chosen, on_chunk=on_chunk, max_tokens=EXTRACT_MAX_TOKENS
            )

        result = extract_from_html(html, page.url or target, chat)
        _tick(report, f"讀完了，抽到 {len(result.records)} 筆。")
        return result

    def save_records(
        self,
        records: Sequence[RawCompany],
        *,
        report: Callable[[object], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> SaveResult:
        """把使用者確認過的那幾筆存進名單。

        走的是爬取那條既有的存檔路徑（:func:`crawler.pipeline.store_records`）
        ——去重、清理、upsert 的規則只該有一份。這裡不自己寫任何 SQL。

        ``cancel_event`` 收下就不用：整批是一個交易，使用者按過確認之後中途
        停下來只會留下一半的資料，比讓它做完糟。簽名要收得下是因為
        :class:`~gui_qt.tasks.BackgroundTask` 一律傳這兩個參數進來。
        """
        from core.constants import CrawlStatus
        from core.schemas import CrawlSummary
        from crawler.pipeline import store_records
        from database.repository import CompanyRepository
        from database.session import session_scope
        from verifier.mx import MXChecker
        from verifier.service import CleaningService

        records = list(records)
        if not records:
            return SaveResult()

        _tick(report, f"正在存 {len(records)} 筆…")
        summary = CrawlSummary(source=AI_SOURCE, status=CrawlStatus.RUNNING.value)
        with session_scope() as session:
            repo = CompanyRepository(session)
            mx = MXChecker(self.config, session) if self.config.verifier.check_mx else None
            store_records(records, repo, CleaningService(self.config, mx), summary)

        # records_duplicate 同時算了「這批自己重複」與「併進既有的」兩種
        # （見 store_records），兩個都報給使用者會變成同一筆講兩次。
        return SaveResult(
            new=summary.records_new,
            merged=summary.records_updated,
            duplicate=max(summary.records_duplicate - summary.records_updated, 0),
            rejected=summary.records_invalid,
        )
