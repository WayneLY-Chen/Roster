"""Page fetching: rate limiting, retries, and two interchangeable engines.

``HttpxFetcher`` handles server-rendered pages and is the default -- it is an
order of magnitude cheaper on both our side and the site's.
``PlaywrightFetcher`` is for directories that render their listings in
JavaScript.

Both enforce the same contract: robots.txt is consulted before every request,
and a polite delay separates requests to the same host.
"""

from __future__ import annotations

import random
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import httpx
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.config import AppConfig, get_config
from core.constants import LogCategory
from core.errors import CrawlError, RobotsDisallowedError
from core.logging_setup import get_logger
from crawler.robots import RobotsPolicy

log = get_logger(LogCategory.CRAWL)

# Status codes worth trying again; everything else is a settled answer.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

# 部分台灣網站宣告的 Big5 其實不是嚴格標準 Big5（罕見姓名用字、擴充符號常
# 超出範圍），因此指定 "big5" 時一律改用其超集 big5hkscs 解碼，涵蓋範圍更廣
# 且對標準 Big5 完全相容；同時一律加上 errors="replace"，讓少數解不出來的
# 字元變成替代字元，而不是讓整頁解碼失敗、白白浪費一次請求。
_ENCODING_ALIASES = {"big5": "big5hkscs"}


def decode_bytes(raw: bytes, encoding: str) -> str:
    """以指定編碼解碼位元組，未知編碼時退回 UTF-8 而不是讓整頁失敗。"""
    codec = _ENCODING_ALIASES.get(encoding.lower(), encoding)
    try:
        return raw.decode(codec, errors="replace")
    except LookupError:
        log.warning("unknown encoding {!r}; falling back to utf-8", encoding)
        return raw.decode("utf-8", errors="replace")


def _decode_body(response: httpx.Response, encoding: str | None) -> str:
    """依指定編碼解碼回應內容；未指定時交給 httpx 自己判斷（標頭或自動偵測）。"""
    if not encoding:
        return response.text
    return decode_bytes(response.content, encoding)


def _encode_form_body(data: dict[str, str], encoding: str) -> bytes:
    """依指定編碼組出 x-www-form-urlencoded 的請求內容（位元組）。

    老舊的 Big5 站台（多半是舊版 ASP/PHP）通常整頁——包含表單提交——都只認
    同一種編碼；httpx 的 ``data=`` 參數固定以 UTF-8 編碼中文字，對這類站台
    送出查詢關鍵字會變成亂碼、查不到資料。指定 ``encoding`` 時改用同一套
    編碼組出請求內容，讓查詢字串與頁面本身用的是同一種編碼。
    """
    codec = _ENCODING_ALIASES.get(encoding.lower(), encoding)
    return urlencode(data, encoding=codec).encode("ascii")


class TransientFetchError(CrawlError):
    """A failure that may resolve on retry (timeout, 503, connection reset)."""


#: 一次 ``click_all`` 最多按幾個元素。一頁 200 列、每列一顆「顯示電話」是常
#: 態，全部按完要花不少時間，但如果一頁有上千個相符元素，那多半是選擇器寫錯了。
MAX_CLICKS_PER_ACTION = 300


def _run_page_actions(page: Any, actions: Sequence[Any]) -> None:
    """在擷取之前，把使用者設定的動作在頁面上做一遍。

    每一個動作都獨立處理失敗：「同意 cookie」那顆按鈕第二頁就不會再出現，
    那是正常的，不該讓整趟爬取停下來。只有標了 ``required`` 的動作失敗才會
    往上丟——那代表使用者說「沒做到這件事，這一頁的資料就是不完整的」。
    """
    for action in actions:
        try:
            _run_one_action(page, action)
        except CrawlError:
            raise
        except Exception as exc:
            if getattr(action, "required", False):
                raise CrawlError(
                    f"頁面動作 {action.type}（{action.selector}）失敗：{exc}"
                ) from exc
            log.debug("頁面動作 {} 略過：{}", action.type, exc)


def _run_one_action(page: Any, action: Any) -> None:
    wait_ms = getattr(action, "wait_ms", 400)
    times = getattr(action, "times", 1)
    selector = (getattr(action, "selector", None) or "").strip()

    if action.type == "wait":
        page.wait_for_timeout(wait_ms)
        return

    if action.type == "scroll":
        for _ in range(times):
            page.mouse.wheel(0, 20_000)
            page.wait_for_timeout(wait_ms)
        return

    if action.type == "click":
        for _ in range(times):
            element = page.query_selector(selector)
            if element is None:
                break          # 「載入更多」按完就消失了，那是做完了不是失敗
            element.click()
            page.wait_for_timeout(wait_ms)
        return

    if action.type == "click_all":
        elements = page.query_selector_all(selector)[:MAX_CLICKS_PER_ACTION]
        for element in elements:
            try:
                element.click()
            except Exception as exc:
                # 一顆按不動（被蓋住、已經展開）不該讓其餘 199 顆都不按。
                log.debug("click_all 有一個元素按不動：{}", exc)
        if elements:
            page.wait_for_timeout(wait_ms)
        return

    log.warning("不認得的頁面動作：{}", action.type)


# ---------------------------------------------------------------- 逐項查詢


def _is_a_select(page: Any, selector: str) -> bool:
    try:
        tag = page.eval_on_selector(selector, "el => el.tagName.toLowerCase()")
    except Exception:                       # noqa: BLE001 - 找不到元素
        return False
    return str(tag).lower() == "select"


def _option_pairs(page: Any, selector: str) -> list[tuple[str, str]]:
    """下拉選單裡每一個真正可以查的 ``(值, 顯示文字)``。

    第一個通常是「--請選擇--」，值是空的；那不是一個查詢條件，是提示文字。

    顯示文字要一起帶回來，因為使用者是照著畫面上看到的東西講話的——他說的是
    「從 03 魚類 爬到 10」，不是「從第 3 個爬到第 10 個」。而 ``value`` 常常
    只是 ``"03"``，甚至是一組沒有意義的編號。
    """
    if not _is_a_select(page, selector):
        return []
    try:
        pairs = page.eval_on_selector_all(
            f"{selector} option",
            "els => els.map(e => [e.value, (e.textContent || '').trim()])",
        )
    except Exception as exc:                # noqa: BLE001
        log.warning("讀不到 {} 的選項：{}", selector, exc)
        return []
    result: list[tuple[str, str]] = []
    for entry in pairs or []:
        # 舊的寫法只問 value，所以這裡兩種形狀都接：一個字串當成「值就是它，
        # 沒有顯示文字」。瀏覽器回來的東西不保證是我們想的樣子，為了一個
        # 選項的形狀讓整趟爬取當掉不值得。
        if isinstance(entry, (list, tuple)):
            value = str(entry[0]) if entry else ""
            label = str(entry[1]) if len(entry) > 1 else ""
        else:
            value, label = str(entry), ""
        if value.strip():
            result.append((value, label))
    return result


def _option_values(page: Any, selector: str) -> list[str]:
    """下拉選單裡每一個真正可以查的選項值。"""
    return [value for value, _label in _option_pairs(page, selector)]


def _match_option(needle: str, pairs: list[tuple[str, str]], *, last: bool) -> int | None:
    """使用者打的字對到第幾個選項；對不到回 ``None``。

    值與顯示文字都比對，而且是「包含」不是「等於」——使用者會打「03」也會打
    「03 魚類」，兩種都要認得。``last`` 是給終點用的：「爬到 10」指的是最後
    一個含「10」的選項，不是第一個。
    """
    needle = needle.strip().lower()
    if not needle:
        return None
    hits = [
        index
        for index, (value, label) in enumerate(pairs)
        if needle in value.strip().lower() or needle in label.strip().lower()
    ]
    if not hits:
        return None
    return hits[-1] if last else hits[0]


def _close_modal(page: Any, modal: Any) -> None:
    close_selector = (getattr(modal, "close_selector", None) or "").strip()
    if close_selector:
        button = page.query_selector(close_selector)
        if button is not None:
            button.click()
            page.wait_for_timeout(200)
            return
    # 沒指定關閉鈕就按 Esc。多數彈出視窗都吃這一招，而且不會誤按到別的東西。
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)


def _collect_modal_details(page: Any, modal: Any, list_selector: str) -> list[str]:
    """把每一列點開，讀出小視窗裡的內容。

    回傳的順序與清單上的每一列**一一對應**，點不開的那一筆留一個空字串。
    這件事非做不可：資料是靠位置對回去的，失敗時如果直接跳過不放，後面每一
    筆的聯絡資訊都會錯位到別人家去——而且看起來完全正常。
    """
    expected = len(page.query_selector_all(list_selector))
    rows = page.query_selector_all(list_selector)[: modal.max_rows]
    details: list[str] = []
    for index, row in enumerate(rows):
        # 頁面在中途被換掉了（有些網站點某一列等於再往下鑽一層）。再點下去
        # 的東西已經不是原本那幾列，硬做下去會把別人的電話掛到這一筆頭上。
        # 停下來、其餘留空——少一點資料，比錯的資料好。
        if len(page.query_selector_all(list_selector)) != expected:
            log.info("清單在讀取詳細視窗的途中變了，其餘幾列不再點開")
            details.extend([""] * (len(rows) - index))
            break
        try:
            target = row.query_selector(modal.click_selector)
            if target is None:
                details.append("")
                continue
            target.click()
            page.wait_for_timeout(modal.wait_ms)
            panel = page.query_selector(modal.panel_selector)
            details.append(panel.inner_html() if panel is not None else "")
            _close_modal(page, modal)
        except Exception as exc:              # noqa: BLE001 - 一筆點不開
            log.debug("第 {} 列的詳細視窗打不開：{}", index + 1, exc)
            details.append("")
    return details


#: 找得出「彈出來的那一塊」的候選寫法。涵蓋 Bootstrap、各家 UI 框架與手寫的。
_PANEL_HINTS = (
    "[role=dialog]", "[class*=modal]", "[class*=dialog]",
    "[class*=popup]", "[class*=lightbox]",
)

#: 在瀏覽器裡跑，回報每一個候選容器的「選擇器、看不看得見、有多少字」。
#:
#: 為什麼要自己算選擇器：Playwright 沒有「把這個元素轉回 CSS 選擇器」的東西，
#: 而我們要存進來源設定的正是那一段字串——爬取時是另一個行程、另一次載入，
#: 手上只會有選擇器，不會有這一次的元素物件。
_PANEL_PROBE_JS = """
(hints) => {
  const escape = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s;
  const out = [];
  for (const el of document.querySelectorAll(hints.join(','))) {
    // <body> 要排除。Bootstrap 開啟彈窗時會把 modal-open 加在 body 上，
    // 它於是符合 [class*=modal]——挑到它的話，每一筆讀到的是整頁的文字。
    if (el === document.body || el === document.documentElement) continue;
    let css = '';
    if (el.id) {
      css = '#' + escape(el.id);
    } else if (typeof el.className === 'string' && el.className.trim()) {
      const classes = el.className.trim().split(/\\s+/).filter(Boolean);
      css = el.tagName.toLowerCase() + classes.map(c => '.' + escape(c)).join('');
    }
    if (!css) continue;
    const box = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    const visible = box.width > 120 && box.height > 60
      && style.display !== 'none' && style.visibility !== 'hidden'
      && style.opacity !== '0';
    out.push({css, visible, chars: (el.innerText || '').trim().length});
  }
  return out;
}
"""

#: 從「真的點得開小視窗的那一列」往上找，收斂成只框住同一張表的選擇器。
#:
#: 為什麼需要：分析挑出來的清單選擇器常常是 ``tr`` 這種很寬的東西，同一頁上
#: 的子分類表、表頭也一起被框進去。爬取時每一列都要點開讀明細，而點到子分類
#: 那一列等於整張表被換掉——只好停手，於是真正的廠商一個明細也讀不到。
#:
#: 往上走到第一個「有 id 或 class、而且框起來會變少」的祖先就停，回傳的選擇器
#: 一定還框得住剛剛點成功的那一列（下面有檢查）。找不到就回空字串，維持原樣。
_ROW_SCOPE_JS = """
(row) => {
  const escape = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s;
  const cssFor = (el) => {
    if (el.id) return '#' + escape(el.id);
    if (typeof el.className === 'string' && el.className.trim()) {
      const cs = el.className.trim().split(/\\s+/).filter(Boolean);
      return el.tagName.toLowerCase() + cs.map(c => '.' + escape(c)).join('');
    }
    return '';
  };
  const tag = row.tagName.toLowerCase();
  const wide = document.querySelectorAll(tag).length;
  let suffix = tag;
  let node = row.parentElement;
  while (node && node !== document.body && node !== document.documentElement) {
    const own = cssFor(node);
    if (own) {
      const scoped = own + ' ' + suffix;
      const hit = document.querySelectorAll(scoped);
      if (hit.length < wide && Array.prototype.indexOf.call(hit, row) !== -1) {
        return scoped;
      }
    }
    suffix = node.tagName.toLowerCase() + ' ' + suffix;
    node = node.parentElement;
  }
  return '';
}
"""

#: 關掉小視窗的那顆按鈕，文字長這樣。
_CLOSE_WORDS = ("確定", "關閉", "close", "ok", "×", "✕", "x")

#: 分析時最多試點幾列來找小視窗。
#:
#: 每一下都是對別人網站的一次互動，所以要有上限；但只試一列不夠——清單選擇器
#: 常常是 ``tr`` 這種很寬的東西，第一列可能是表頭，也可能是「還要再點一層」的
#: 子分類（ieatpe 就是），點下去是往下鑽而不是開視窗。
_MODAL_PROBE_ROWS = 4


def _panels(page: Any) -> dict[str, dict[str, Any]]:
    """目前頁面上每一個候選彈出容器的狀態，以選擇器為 key。"""
    try:
        found = page.evaluate(_PANEL_PROBE_JS, list(_PANEL_HINTS))
    except Exception as exc:                  # noqa: BLE001
        log.debug("讀不到頁面上的彈出容器：{}", exc)
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in found or []:
        css = str(item.get("css") or "")
        if css and css not in result:
            result[css] = item
    return result


def _find_close_button(page: Any, panel_selector: str) -> str | None:
    """小視窗裡那顆關閉鈕的選擇器；找不到就回 ``None``（改按 Esc）。

    盡量找得到它：Esc 不是每個網站都吃，而關不掉的話下一列點下去會被這一個
    蓋住——結果是第 2 筆之後全部讀到同一份內容，而且看起來完全正常。
    """
    for candidate in ("button", "a", "[class*=close]"):
        selector = f"{panel_selector} {candidate}"
        try:
            elements = page.query_selector_all(selector)
        except Exception:                     # noqa: BLE001
            continue
        for element in elements:
            try:
                text = (element.inner_text() or "").strip().lower()
                classes = (element.get_attribute("class") or "").lower()
            except Exception:                 # noqa: BLE001
                continue
            if any(word in text for word in _CLOSE_WORDS) or "close" in classes:
                return selector
    return None


def _detect_detail_modal(
    page: Any, list_selector: str, click_selector: str = "a"
) -> dict[str, Any] | None:
    """點第一列看看會不會跳出小視窗，會的話把它的設定描述出來。

    有一整類名錄的明細沒有網址：清單上只有公司名稱，電話、信箱、負責人全在
    「點一下跳出來的那一塊」裡面。不做這一步的話，抓回來的每一筆都只有名字
    ——資料看起來有進來，實際上全是空的。

    判斷方式是「點之前看不見、點之後看得見」，而不是「頁面上有沒有 class 帶
    modal 的東西」：那種容器在很多網站上一直都在（藏著），光看存不存在會把
    cookie 提示、登入視窗都認成明細。

    認出來之後還要確認**裡面真的是聯絡資料**——用讀名錄那一套去拆，至少要拆得
    出兩個看得懂的欄位。少了這一關，一個純圖片的廣告彈窗也會被存成明細設定。
    """
    # 不能只試第一列。清單選擇器常常是 ``tr`` 這種很寬的東西，而同一頁上可能
    # 還有表頭、或是「還要再點一層」的子分類表——ieatpe 的第一個 tr 點下去是
    # 往下鑽，不是開小視窗。試了才知道，所以多試幾列。
    #
    # 而且「點了沒有小視窗」本身常常是有進展的：那一下把子分類換成了廠商清單，
    # 下一次試就點得到真正的公司了。所以每一輪都重新查一次列。
    appeared: list[tuple[str, dict[str, Any]]] = []
    opened_row: Any = None
    for attempt in range(_MODAL_PROBE_ROWS):
        rows = page.query_selector_all(list_selector)
        if not rows:
            return None
        target = None
        for row in rows[attempt:]:
            found = row.query_selector(click_selector)
            if found is not None:
                target, opened_row = found, row
                break
        if target is None:
            return None

        before = _panels(page)
        try:
            _click_even_if_hidden(target)
            page.wait_for_timeout(900)
        except Exception as exc:              # noqa: BLE001
            log.debug("第 {} 次試點失敗：{}", attempt + 1, exc)
            return None

        after = _panels(page)
        appeared = [
            (css, item)
            for css, item in after.items()
            if item.get("visible") and not before.get(css, {}).get("visible")
        ]
        if appeared:
            break
    if not appeared:
        return None

    # 挑「最小的、但裡面確實有聯絡資料」的那一層。
    #
    # 一個彈窗通常是好幾層巢狀的容器（.modal > .modal-dialog > .modal-content
    # > .modal-body），字數由外而內遞減但內容差不多。挑最大的那一層等於把外框
    # 與按鈕文字一起讀進來；由小到大找第一個拆得出欄位的，拿到的才是真正裝
    # 資料的那一塊。
    from crawler.labels import parse_record

    panel_selector = ""
    record = None
    for css, _item in sorted(appeared, key=lambda pair: pair[1].get("chars", 0)):
        panel = page.query_selector(css)
        if panel is None:
            continue
        try:
            text = panel.inner_text() or ""
        except Exception:                     # noqa: BLE001
            continue
        candidate = parse_record(text)
        if candidate.pair_count >= 2:
            panel_selector, record = css, candidate
            break

    if record is None:
        log.info("點開了一塊東西，但裡面看不出是聯絡資料，不當成明細視窗")
        return None

    # 關閉鈕不一定在讀資料的那一塊裡面——Bootstrap 的「確定」在 .modal-footer，
    # 跟 .modal-body 是兄弟。所以整組彈出來的容器都找一遍，由外而內。
    #
    # 找得到很重要：Esc 不是每個網站都吃，關不掉的話下一列點下去會被這一個
    # 蓋住，第 2 筆之後全部讀到同一份內容，而且看起來完全正常。
    close_selector = None
    for css, _item in sorted(
        appeared, key=lambda pair: pair[1].get("chars", 0), reverse=True
    ):
        close_selector = _find_close_button(page, css)
        if close_selector:
            break
    log.info(
        "找到明細小視窗：{}（拆得出 {} 個欄位）", panel_selector, record.pair_count
    )
    return {
        "click_selector": click_selector,
        "panel_selector": panel_selector,
        "close_selector": close_selector,
        "sample_fields": sorted(record.fields),
        "row_selector": _scope_to_the_row_that_worked(page, opened_row, list_selector),
    }


def _scope_to_the_row_that_worked(
    page: Any, row: Any, list_selector: str
) -> str | None:
    """把清單選擇器收斂到「剛剛真的點得開小視窗的那一列」所在的那一張表。

    回傳 ``None`` 代表沒得收斂（原本就夠窄，或找不到有 id/class 的祖先），
    照原樣用即可。
    """
    if row is None:
        return None
    try:
        scoped = row.evaluate(_ROW_SCOPE_JS)
    except Exception as exc:                  # noqa: BLE001
        log.debug("收斂清單選擇器失敗：{}", exc)
        return None
    scoped = str(scoped or "").strip()
    if not scoped or scoped == list_selector:
        return None
    try:
        narrowed = len(page.query_selector_all(scoped))
        original = len(page.query_selector_all(list_selector))
    except Exception:                         # noqa: BLE001
        return None
    if not narrowed or narrowed >= original:
        return None
    log.info(
        "清單選擇器收斂：{}（{} 列）→ {}（{} 列）",
        list_selector, original, scoped, narrowed,
    )
    return scoped


def _submit_one_query(page: Any, loop: Any, value: str) -> None:
    """填一個條件並按下查詢。"""
    selector = loop.input_selector
    if _is_a_select(page, selector):
        # force=True 是必要的。很多網站把原生的 <select> 藏起來，畫面上那個好看
        # 的下拉是自己用 div 做的——原生的那個永遠「看不見」，不加這個參數會一直
        # 等到逾時。我們要改的本來就是原生元素的值，選好之後照樣會送出 change
        # 事件，網站的程式收得到。
        page.select_option(selector, value, force=True)
    else:
        page.fill(selector, value, force=True)

    button = page.query_selector(loop.submit_selector)
    if button is None:
        raise CrawlError(f"找不到查詢按鈕：{loop.submit_selector}")
    _click_even_if_hidden(button)
    page.wait_for_timeout(loop.wait_ms)


def _click_even_if_hidden(element: Any) -> None:
    """按下去，元素被藏起來也要按到。

    查詢頁常常做成分頁籤，沒被選到的那一頁是 ``display:none``——裡面的按鈕在
    畫面上不存在，一般的點擊會一直等到逾時。改用送出 click 事件的方式，網站
    自己的程式收到的東西是一樣的。
    """
    try:
        element.click()
    except Exception as exc:                  # noqa: BLE001
        log.debug("一般點擊失敗，改用送出事件的方式：{}", exc)
        element.dispatch_event("click")


@dataclass(slots=True)
class FetchResult:
    """One retrieved page."""

    url: str
    status_code: int
    html: str
    elapsed: float = 0.0
    from_cache: bool = False
    #: 未經解碼的原始回應內容。分析網址時用得到：頁面可能在 HTML 裡宣告了
    #: 一個跟 HTTP 標頭不同的編碼（老舊的 Big5 站台幾乎都是這樣），留著原始
    #: 位元組就能直接換編碼重解一次，不必為了同一頁再送一次請求。
    #: Playwright 引擎沒有這個東西——它交出來的是瀏覽器解碼後的 DOM。
    raw: bytes = b""
    #: 逐列點開的小視窗內容，順序與清單上的每一列一一對應。
    #: 只有來源設了 ``detail_modal`` 時才會有東西。
    details: list[str] = field(default_factory=list)
    #: 分析時試點第一列的結果：有沒有跳出明細小視窗，以及它的選擇器。
    #: 只有分析階段（``probe_modal_for``）才會有東西，正常爬取時是 ``None``。
    modal_probe: "dict[str, Any] | None" = None
    #: 逐項查詢用：到這一份結果為止，**已經整個做完**幾個查詢條件。
    #: 中斷續跑靠它，所以它只在一個條件底下的東西全部處理完才前進。
    completed_values: int | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


@dataclass
class RateLimiter:
    """Enforces a minimum gap between requests, with jitter.

    Jitter matters: a perfectly periodic crawler looks like an attack to rate
    limiters, and it synchronizes badly with server-side bucket refills.
    """

    delay: float = 2.0
    jitter: float = 0.5
    _last_request: float = field(default=0.0, init=False)

    def wait(self, minimum: float | None = None) -> float:
        """Sleep until the next request is due. Returns seconds slept."""
        target = max(self.delay, minimum or 0.0)
        if target <= 0:
            self._last_request = time.monotonic()
            return 0.0

        target += random.uniform(0, self.jitter)
        elapsed = time.monotonic() - self._last_request
        remaining = target - elapsed
        if self._last_request and remaining > 0:
            time.sleep(remaining)
            slept = remaining
        else:
            slept = 0.0
        self._last_request = time.monotonic()
        return slept


class BaseFetcher(ABC):
    """Common retry, rate-limit and robots handling for both engines."""

    def __init__(self, config: AppConfig | None = None, robots: RobotsPolicy | None = None) -> None:
        self.config = config or get_config()
        self.user_agent = self.config.crawler.resolved_user_agent()
        self.robots = robots or RobotsPolicy(
            user_agent=self.user_agent,
            timeout=self.config.crawler.request_timeout,
            enabled=self.config.crawler.respect_robots,
        )
        self.limiter = RateLimiter(
            delay=self.config.crawler.delay_seconds,
            jitter=self.config.crawler.delay_jitter,
        )

    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        data: dict[str, str] | None = None,
        encoding: str | None = None,
        actions: Sequence[Any] = (),
        modal: Any = None,
        list_selector: str | None = None,
    ) -> FetchResult:
        """Fetch one URL, honouring robots.txt, the delay, and the retry budget.

        ``method``/``data`` cover POST-only search forms (robots.txt is still
        consulted first, exactly as for GET); ``encoding`` forces decoding the
        response with a specific charset for sites whose headers are wrong or
        absent, and -- for POST -- also encodes the outgoing form body with
        the same charset, since old same-charset-both-ways sites often reject
        (or silently mismatch) a UTF-8 query string.
        """
        if not self.robots.can_fetch(url):
            raise RobotsDisallowedError(url, self.user_agent)

        self.limiter.wait(minimum=self.robots.crawl_delay(url))

        retryer = Retrying(
            stop=stop_after_attempt(self.config.crawler.max_retries + 1),
            wait=wait_exponential(
                multiplier=self.config.crawler.retry_backoff, min=1, max=60
            ),
            retry=retry_if_exception_type(TransientFetchError),
            reraise=True,
            before_sleep=lambda state: log.warning(
                "retry {}/{} for {}: {}",
                state.attempt_number,
                self.config.crawler.max_retries,
                url,
                state.outcome.exception() if state.outcome else "unknown",
            ),
        )
        started = time.monotonic()
        result = retryer(
            self._fetch_once, url, method=method, data=data,
            encoding=encoding, actions=actions,
            modal=modal, list_selector=list_selector,
        )
        result.elapsed = time.monotonic() - started
        log.debug("fetched {} [{}] in {:.2f}s", url, result.status_code, result.elapsed)
        return result

    @abstractmethod
    def _fetch_once(
        self,
        url: str,
        *,
        method: str = "GET",
        data: dict[str, str] | None = None,
        encoding: str | None = None,
        actions: Sequence[Any] = (),
        modal: Any = None,
        list_selector: str | None = None,
    ) -> FetchResult:
        """Single attempt. Raise :class:`TransientFetchError` to trigger retry."""

    def close(self) -> None:
        self.robots.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class HttpxFetcher(BaseFetcher):
    """HTTP fetcher for server-rendered pages."""

    def __init__(
        self,
        config: AppConfig | None = None,
        robots: RobotsPolicy | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(config, robots)
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=self.config.crawler.request_timeout,
            follow_redirects=True,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            },
        )

    def _fetch_once(
        self,
        url: str,
        *,
        method: str = "GET",
        data: dict[str, str] | None = None,
        encoding: str | None = None,
        actions: Sequence[Any] = (),
        modal: Any = None,
        list_selector: str | None = None,
    ) -> FetchResult:
        if modal is not None:
            # 安靜地忽略等於使用者永遠看不到「怎麼還是沒有電話」的原因。
            log.warning(
                "這個來源要點開小視窗才看得到詳細資料，但取頁面的方式是 httpx，"
                "點不了。這個來源的引擎要設成 playwright。"
            )
        try:
            if method == "POST":
                # 表單以 application/x-www-form-urlencoded 送出，這是傳統
                # ASP/PHP 查詢頁最常見的格式。有指定 encoding 時，連同送出
                # 的表單內容也用同一種編碼組——像 TCA 這類 Big5 年代的舊站，
                # 查詢關鍵字若用 httpx 預設的 UTF-8 送出會直接查不到資料，
                # 因為伺服器是拿 Big5 位元組去比對資料庫。沒指定 encoding
                # 時維持原本用 httpx 預設（UTF-8）的行為，不影響既有來源。
                if encoding:
                    response = self._client.post(
                        url,
                        content=_encode_form_body(data or {}, encoding),
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
                else:
                    response = self._client.post(url, data=data or {})
            else:
                response = self._client.get(url)
        except httpx.TimeoutException as exc:
            raise TransientFetchError(f"timeout fetching {url}") from exc
        except httpx.TransportError as exc:
            raise TransientFetchError(f"transport error fetching {url}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise CrawlError(f"failed to fetch {url}: {exc}") from exc

        if response.status_code in _RETRYABLE_STATUS:
            self._honour_retry_after(response)
            raise TransientFetchError(f"{url} returned {response.status_code}")

        if response.status_code >= 400:
            raise CrawlError(f"{url} returned {response.status_code}")

        return FetchResult(
            url=str(response.url),
            status_code=response.status_code,
            html=_decode_body(response, encoding),
            raw=response.content,
        )

    @staticmethod
    def _honour_retry_after(response: httpx.Response) -> None:
        """Sleep for a server-specified Retry-After, capped so we never hang."""
        header = response.headers.get("Retry-After")
        if not header:
            return
        try:
            seconds = float(header)
        except ValueError:
            return  # HTTP-date form; the exponential backoff covers it
        wait = min(max(seconds, 0.0), 60.0)
        log.info("server asked us to wait {:.0f}s before retrying", wait)
        time.sleep(wait)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
        super().close()


class PlaywrightFetcher(BaseFetcher):
    """Headless-browser fetcher for JavaScript-rendered listings.

    Requires a one-off browser download::

        python -m playwright install chromium
    """

    def __init__(self, config: AppConfig | None = None, robots: RobotsPolicy | None = None) -> None:
        super().__init__(config, robots)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise CrawlError(
                "playwright is not installed; run: pip install playwright"
            ) from exc

        self._settings = self.config.crawler.playwright
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                headless=self._settings.headless
            )
        except Exception as exc:
            raise CrawlError(
                "could not start Chromium. Install the browser once with: "
                f"python -m playwright install chromium ({exc})"
            ) from exc

        self._context = self._browser.new_context(
            user_agent=self.user_agent,
            locale="zh-TW",
        )
        self._context.set_default_navigation_timeout(self._settings.nav_timeout_ms)

    def _fetch_once(
        self,
        url: str,
        *,
        method: str = "GET",
        data: dict[str, str] | None = None,
        encoding: str | None = None,
        actions: Sequence[Any] = (),
        modal: Any = None,
        list_selector: str | None = None,
    ) -> FetchResult:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        if method != "GET":
            # 瀏覽器只會「導覽」到網址；沒有簡單的方式送出任意 POST 表單並
            # 維持一般的頁面渲染流程，所以需要 POST 表單的來源請改用
            # engine: httpx。
            raise CrawlError(
                "Playwright 引擎不支援 method='POST'；此來源請改用 engine: httpx"
            )

        page = self._context.new_page()
        try:
            response = page.goto(url, wait_until=self._settings.wait_until)
            status = response.status if response else 0
            if status in _RETRYABLE_STATUS:
                raise TransientFetchError(f"{url} returned {status}")
            if status >= 400:
                raise CrawlError(f"{url} returned {status}")
            if actions:
                _run_page_actions(page, actions)
            details = (
                _collect_modal_details(page, modal, list_selector or "")
                if modal is not None and list_selector
                else []
            )
            return FetchResult(
                url=page.url,
                status_code=status or 200,
                html=page.content(),
                details=details,
            )
        except PlaywrightTimeout as exc:
            raise TransientFetchError(f"timeout loading {url}") from exc
        except PlaywrightError as exc:
            raise TransientFetchError(f"browser error loading {url}: {exc}") from exc
        finally:
            page.close()

    # --------------------------------------------------------- 逐項查詢

    def fetch_with_first_query(
        self,
        url: str,
        input_selector: str,
        submit_selector: str,
        *,
        value: str | None = None,
        drill_row_selector: str | None = None,
        drill_click_selector: str = "a",
        probe_modal_for: str | None = None,
    ) -> FetchResult:
        """開頁面、送出**一次**查詢，回傳結果的 HTML。

        分析查詢型名錄時用得到：還沒查詢的頁面上一筆資料都沒有，只看那一頁是
        猜不出「一筆資料長什麼樣」的。送一次查詢再看，跟人打開網頁隨便選一個
        分類按下去是完全一樣的動作。

        ``drill_row_selector`` 再往下點一層：有些網站查出來的是子分類，點其中
        一項才是廠商。給了就會點第一列，回傳那之後的頁面。
        """
        if not self.robots.can_fetch(url):
            raise RobotsDisallowedError(url, self.user_agent)

        page = self._context.new_page()
        try:
            self.limiter.wait(minimum=self.robots.crawl_delay(url))
            page.goto(url, wait_until=self._settings.wait_until)

            chosen = value
            if chosen is None:
                options = _option_values(page, input_selector)
                if not options:
                    raise CrawlError(f"{input_selector} 沒有可以選的選項")
                chosen = options[0]

            loop = SimpleNamespace(
                input_selector=input_selector,
                submit_selector=submit_selector,
                wait_ms=1500,
            )
            self.limiter.wait(minimum=self.robots.crawl_delay(url))
            _submit_one_query(page, loop, chosen)

            if drill_row_selector:
                rows = page.query_selector_all(drill_row_selector)
                target = None
                for row in rows:
                    target = row.query_selector(drill_click_selector)
                    if target is not None:
                        break
                if target is None:
                    raise CrawlError(f"{drill_row_selector} 裡面沒有可以點的東西")
                self.limiter.wait(minimum=self.robots.crawl_delay(url))
                _click_even_if_hidden(target)
                page.wait_for_timeout(1500)

            html = page.content()
            # 明細小視窗要在**這一頁**上試，而且要在讀完 HTML 之後——點下去
            # 會蓋住畫面，先讀才不會把彈窗的內容混進清單。這一步不另外開頁面，
            # 所以不多送一次請求。
            probe = None
            if probe_modal_for:
                self.limiter.wait(minimum=self.robots.crawl_delay(url))
                probe = _detect_detail_modal(page, probe_modal_for)

            return FetchResult(
                url=page.url, status_code=200, html=html, modal_probe=probe
            )
        finally:
            page.close()

    def probe_detail_modal(
        self, url: str, list_selector: str, click_selector: str = "a"
    ) -> dict[str, Any] | None:
        """開一個不需要查詢的清單頁，試點第一列看有沒有明細小視窗。

        查詢型的名錄走 :meth:`fetch_with_first_query` 的 ``probe_modal_for``
        （那裡已經在正確的那一頁上了）；這個是給「網址打開就是清單」的站用的。
        """
        if not self.robots.can_fetch(url):
            raise RobotsDisallowedError(url, self.user_agent)

        page = self._context.new_page()
        try:
            self.limiter.wait(minimum=self.robots.crawl_delay(url))
            page.goto(url, wait_until=self._settings.wait_until)
            return _detect_detail_modal(page, list_selector, click_selector)
        finally:
            page.close()

    def iter_query_pages(
        self,
        url: str,
        loop: Any,
        *,
        actions: Sequence[Any] = (),
        cancel_event: Any = None,
        modal: Any = None,
        list_selector: str | None = None,
        skip_values: int = 0,
    ) -> Iterator[FetchResult]:
        """開一次頁面，把每一組查詢條件各查一次，每查一次交出一份結果 HTML。

        為什麼是一個產生器而不是「查完全部再回傳」：一輪就是幾百家公司，全部
        累積在記憶體裡等到最後才處理，中途取消或出錯就整批損失。

        整段只導覽一次網址——查詢是在同一個頁面裡進行的，這也是它比「一頁一次
        請求」對別人的伺服器更客氣的地方。每一輪之間仍然照設定的間隔等待。
        """
        if not self.robots.can_fetch(url):
            raise RobotsDisallowedError(url, self.user_agent)

        page = self._context.new_page()
        try:
            self.limiter.wait(minimum=self.robots.crawl_delay(url))
            page.goto(url, wait_until=self._settings.wait_until)
            if actions:
                _run_page_actions(page, actions)

            pairs = _option_pairs(page, loop.input_selector)
            values = list(loop.values) or [value for value, _ in pairs]
            if not values:
                raise CrawlError(
                    f"逐項查詢找不到任何可以查的值：{loop.input_selector} "
                    "既不是下拉選單，來源也沒有指定要查哪些值。"
                )

            # 使用者指定的起訖。97 個分類跑完要好幾個小時，本來就會分次跑
            # ——今天 1 到 20，明天從 21 接下去。這一段先套，續跑的進度才是
            # 「從這個起點之後又做完幾個」，兩者不會互相蓋掉。
            #
            # 起訖可以是序號，也可以是選項上的文字：使用者看著畫面說的是
            # 「從 03 魚類 爬到 10」，沒有人會去數那是第幾個。
            start_index = max(0, int(getattr(loop, "start_at", 1) or 1) - 1)
            count = loop.max_queries

            start_text = str(getattr(loop, "start_value", "") or "")
            matched = _match_option(start_text, pairs, last=False) if pairs else None
            if start_text and matched is None:
                log.warning("找不到起點「{}」這個選項，改從第一個開始", start_text)
            elif matched is not None:
                start_index = matched

            end_text = str(getattr(loop, "end_value", "") or "")
            end_index = _match_option(end_text, pairs, last=True) if pairs else None
            if end_text and end_index is None:
                log.warning("找不到終點「{}」這個選項，改用「查幾個」的設定", end_text)
            elif end_index is not None:
                count = max(1, end_index - start_index + 1)

            if start_index or count != loop.max_queries:
                log.info(
                    "逐項查詢從第 {} 個條件開始，共查 {} 個", start_index + 1, count
                )
            values = values[start_index:]

            # 接續上一次：前面那些條件已經整個做完了，不必再查一遍。
            if skip_values:
                log.info("逐項查詢接續上一次，跳過前 {} 個條件", skip_values)
                values = values[skip_values:]

            # 已經**整個做完**幾個條件。中斷續跑靠它，所以它只在一個條件底下的
            # 東西全部處理完之後才前進——不然接續時會跳過還沒點完的那一個。
            completed = 0
            for value in values[:count]:
                if cancel_event is not None and cancel_event.is_set():
                    return
                self.limiter.wait(minimum=self.robots.crawl_delay(url))
                try:
                    _submit_one_query(page, loop, value)
                except Exception as exc:      # noqa: BLE001 - 一個條件查壞了
                    # 不要讓 98 個分類裡的第 7 個失敗，害其餘 91 個都收不到。
                    log.warning("逐項查詢「{}」失敗：{}", value, exc)
                    completed += 1   # 查壞的也算走過了，續跑不要卡在它身上
                    continue
                drill = getattr(loop, "drill", None)
                if drill is None:
                    yield self._snapshot(page, modal, list_selector, completed)
                    completed += 1
                    continue

                # 查詢結果還不是名單，中間要再點一層。
                for index in range(drill.max_rows):
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    # 每一列都要從「剛查完」的那一頁重新點下去。
                    #
                    # 點第一列會把畫面換成那個子分類底下的廠商清單——留在那裡
                    # 去點「第 2 列」，點到的是**廠商**，開出來的是明細小視窗，
                    # 不是下一個子分類。結果就是第 2 批之後每一筆都是同一家
                    # 公司、而且一個欄位都沒有。實測遇過。
                    #
                    # 重查一次比按上一頁可靠：這一類頁面多半是表單回傳（每一次
                    # 動作都是一次 POST），上一頁常常拿到的是過期或空的畫面。
                    if index:
                        self.limiter.wait(minimum=self.robots.crawl_delay(url))
                        try:
                            _submit_one_query(page, loop, value)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("回到查詢結果失敗，這個條件先到這裡：{}", exc)
                            break
                    # 每一次都重新取一遍列：點下去之後整張表常常被重畫，
                    # 先存起來的那些元素會全部失效（stale）。
                    rows = page.query_selector_all(drill.row_selector)
                    if index >= len(rows):
                        break
                    self.limiter.wait(minimum=self.robots.crawl_delay(url))
                    try:
                        target = rows[index].query_selector(drill.click_selector)
                        if target is None:
                            continue
                        _click_even_if_hidden(target)
                        page.wait_for_timeout(drill.wait_ms)
                    except Exception as exc:  # noqa: BLE001 - 一列點不開
                        log.debug("往下點第 {} 列失敗：{}", index + 1, exc)
                        continue
                    yield self._snapshot(page, modal, list_selector, completed)
                completed += 1
        finally:
            page.close()

    def _snapshot(
        self,
        page: Any,
        modal: Any,
        list_selector: str | None,
        completed: int | None = None,
    ) -> FetchResult:
        """把頁面目前的狀態交出去（需要的話連同每一列點開的小視窗）。

        **HTML 一定要先讀。** 點開小視窗會動到 DOM——有些網站點下去等於再往下
        鑽一層，整張表被換掉。先收集再讀 HTML 的話，交出去的兩份東西是頁面在
        不同時刻的樣子，而詳細資料是靠**位置**對回每一列的：第 2 筆的電話會
        掛到第 5 筆頭上，或者整批對不上而全部落空。實測遇過（負責人全是空的）。
        """
        html = page.content()
        details = (
            _collect_modal_details(page, modal, list_selector or "")
            if modal is not None and list_selector
            else []
        )
        return FetchResult(
            url=page.url,
            status_code=200,
            html=html,
            details=details,
            completed_values=completed,
        )

    def close(self) -> None:
        for closer in (
            getattr(self, "_context", None),
            getattr(self, "_browser", None),
        ):
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # pragma: no cover - best-effort teardown
                pass
        try:
            if getattr(self, "_playwright", None) is not None:
                self._playwright.stop()
        except Exception:  # pragma: no cover
            pass
        super().close()


def build_fetcher(
    config: AppConfig | None = None,
    robots: RobotsPolicy | None = None,
    engine: str | None = None,
) -> BaseFetcher:
    """Instantiate a fetcher.

    ``engine`` 是這一個來源自己指定的引擎；留空才回頭看全域的
    ``crawler.engine``。「這個網站要不要用瀏覽器」是網站的性質，不是使用者的
    偏好設定——把它綁在來源上，一個需要瀏覽器的網站就不會拖慢其他所有來源。
    """
    config = config or get_config()
    chosen = engine or config.crawler.engine
    if chosen == "playwright":
        return PlaywrightFetcher(config, robots)
    return HttpxFetcher(config, robots)
