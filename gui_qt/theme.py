"""字型與 QSS 樣式。

customtkinter 版本（``gui/fonts.py``）在建立任何 widget 之前，把 Tk 的具名
字型全部換成一個有完整繁體中文字符的字型——不然中文會被 Tk 換成備援字型，
量出來的欄寬跟著跑掉，排版就整個歪掉。

Qt 沒有「具名字型」這個東西，做法簡單很多：直接把 ``QApplication`` 的預設
``QFont`` 換掉，所有沒有另外指定字型的 widget 就會自動套用——不需要對每個
widget 個別設定，也不需要在意 ttk 的具名字型（Qt 沒有 ttk）。

亮／暗主題透過一份 QSS 字串整份套用在 ``QApplication`` 上，對應
``config.yaml`` 的 ``app.theme``：

    system -- 跟隨作業系統（Qt 6.5+ 用 ``styleHints().colorScheme()`` 偵測，
              偵測不到就當作亮色）
    light  -- 固定亮色
    dark   -- 固定暗色

顏色沿用 ``gui/widgets.py`` 裡同一組 (亮, 暗) 色票，兩套介面的視覺才不會
各走各的；之後其他頁面需要顏色時，從這裡 import，不要自己現刻一份。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QPointF, QStandardPaths, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPainter, QPixmap, QPolygonF
from PySide6.QtWidgets import QApplication

from core.constants import LogCategory
from core.logging_setup import get_logger

log = get_logger(LogCategory.GUI)

#: 由上而下試，用第一個系統裝得到的。跟 gui/fonts.py 的 PREFERRED_FAMILIES 一致。
PREFERRED_FAMILIES: tuple[str, ...] = (
    "Microsoft JhengHei UI",
    "Microsoft JhengHei",
    "Noto Sans TC",
    "Microsoft YaHei UI",
    "PingFang TC",          # macOS
    "Noto Sans CJK TC",     # Linux
    "MingLiU",
)

#: 等寬字型，日誌頁移植後會用到。
PREFERRED_MONO: tuple[str, ...] = (
    "Cascadia Mono",
    "Consolas",
    "Courier New",
)

_resolved: str | None = None
_resolved_mono: str | None = None
_current_mode: str = "light"


def _first_available(candidates: tuple[str, ...], installed: set[str]) -> str | None:
    return next((name for name in candidates if name in installed), None)


def configure_fonts(app: QApplication) -> str:
    """挑一個有中文字符的字型，套用到整個 QApplication。回傳實際選用的字族名稱。

    必須在建立任何視窗／widget 之前呼叫（在 ``QApplication`` 建好之後即可）。
    """
    global _resolved, _resolved_mono

    try:
        installed = set(QFontDatabase.families())
    except Exception as exc:  # pragma: no cover - 需要 Qt 平台外掛正常載入
        log.warning("無法列出系統字型，沿用預設值：{}", exc)
        return app.font().family()

    family = _first_available(PREFERRED_FAMILIES, installed)
    if family is None:
        log.warning("找不到任何中文字型，中文可能顯示為方框。")
        family = app.font().family()

    font = QFont(family)
    font.setPointSize(10)
    app.setFont(font)

    _resolved = family
    _resolved_mono = _first_available(PREFERRED_MONO, installed) or family

    log.info("介面字型：{}（等寬：{}）", family, _resolved_mono)
    return family


def ui_family() -> str:
    """目前使用的介面字族。"""
    return _resolved or QApplication.font().family()


def mono_family() -> str:
    """目前使用的等寬字族。"""
    return _resolved_mono or ui_family()


# ---------------------------------------------------------------- 顏色 / 主題

# 與 gui/widgets.py 完全同步的 (亮, 暗) 色票，index 0 是亮色、1 是暗色。
#
# ``WINDOW_BG``／``CARD_FG`` 刻意不是同一種白：``WINDOW_BG`` 是「頁面／輸入框
# 凹陷處」的底色，``CARD_FG`` 是卡片／側邊欄／狀態列這些「浮起來」的表面色。
# 亮色主題頁面底稍微偏灰、卡片維持純白，暗色主題則是卡片比頁面底稍亮——
# 兩套主題用同一個設計邏輯（卡片永遠比頁面底「亮一階」），層次才是刻意設計、
# 不是碰巧。舊版兩個顏色幾乎同色（``#ffffff`` vs ``#f4f6f9``），卡片、表格、
# 側邊欄、狀態列彼此看起來才會像「同一片白」。
CARD_FG = ("#ffffff", "#2c2f36")
ACCENT = ("#1f6aa5", "#2b7bbd")
#: 主要按鈕的 hover／pressed：亮色主題稍微加深、暗色主題稍微加亮，兩者都是
#: 「往更飽和/更深的方向」變化，讓按下去的回饋方向感一致。
ACCENT_HOVER = ("#1b5c8f", "#3a8bce")
ACCENT_PRESSED = ("#164a75", "#20618f")
#: 暗色主題的 muted 比第一版（``#8d95a3``）稍微加亮：那個值對卡片色只有
#: 4.44:1 對比（略低於 WCAG AA 文字門檻 4.5:1），停用按鈕的文字因此看起來
#: 像「壞掉」而不是「刻意變淡」——加亮到 ``#9aa1ae`` 後對卡片色是 5.16:1，
#: 跟一般文字（10.9:1）仍有明顯落差，看得出是刻意的次要文字。
MUTED = ("#5b6270", "#9aa1ae")
DANGER = ("#b3261e", "#f2b8b5")
DANGER_PRESSED = ("#8f1e18", "#c99490")
SUCCESS = ("#1f7a3f", "#6ddc95")
WINDOW_BG = ("#eef1f6", "#1d1e22")
TEXT_FG = ("#1a1c20", "#e6e8ec")
BORDER = ("#d7dce3", "#3a3d44")
HOVER = ("#e2e6ec", "#33363d")
TABLE_STRIPE = ("#e7ebf1", "#2d3037")
#: 捲軸滑塊：預設用中性灰、hover 時加深/加亮，寬度統一在 stylesheet() 裡設定。
SCROLLBAR_HANDLE = ("#c6cbd4", "#494c53")
SCROLLBAR_HANDLE_HOVER = ("#aab0ba", "#5a5e66")


# ------------------------------------------------------------ 小圖示（箭頭/打勾）
#
# Qt 的樣式表沒有「用 CSS 畫三角形/打勾」這回事。這裡有兩個做法都實測失敗過：
#
#   1. 「``width:0；height:0`` 配不對稱 border」畫三角形的經典技巧——在
#      ``QComboBox::down-arrow``／``QSpinBox`` 的增減鍵頭這幾個 subcontrol 上
#      完全不會畫出三角形，只會填滿一塊實心方塊（親自截圖驗證過）。
#   2. ``image: url(data:image/png;base64,...)``——Qt 的樣式表解析器不吃
#      data URI，直接什麼都不畫（同樣截圖驗證過）。
#
# 唯一測過真的有效的作法：把小圖示存成磁碟上的真實 PNG 檔，用絕對路徑的
# ``url(...)`` 參照。以下函式把這幾個小圖示畫出來、快取到系統的快取目錄，
# 檔名帶顏色做 key，顏色不變就不用重畫。

_ICON_SIZE = 12


def _icon_cache_dir() -> Path:
    """存放產生好的小圖示 PNG 的目錄，跨次啟動可以重複使用、不用每次重畫。"""
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
    if not base:
        base = tempfile.gettempdir()  # pragma: no cover - 極少數平台問不到快取目錄時的保險
    path = Path(base) / "qss-icons"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _save_icon(name: str, painter_fn) -> str:
    """如果快取裡沒有這個圖示就畫一張，回傳給 QSS ``url(...)`` 用的絕對路徑。

    ``painter_fn`` 收一個已經 ``begin()`` 好、畫布是 :data:`_ICON_SIZE` 見方
    透明背景的 ``QPainter``，畫完不用自己 ``end()``（這裡統一收尾）。
    """
    path = _icon_cache_dir() / f"{name}.png"
    if not path.exists():
        pixmap = QPixmap(_ICON_SIZE, _ICON_SIZE)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter_fn(painter)
        painter.end()
        pixmap.save(str(path), "PNG")
    return path.as_posix()


def _triangle_icon(color: str, direction: str) -> str:
    """下拉箭頭／QSpinBox 增減鍵頭用的小三角形，``direction`` 是 ``"up"``/``"down"``。"""

    def draw(painter: QPainter) -> None:
        painter.setBrush(QColor(color))
        if direction == "up":
            polygon = QPolygonF([QPointF(2, 8), QPointF(10, 8), QPointF(6, 3)])
        else:
            polygon = QPolygonF([QPointF(2, 4), QPointF(10, 4), QPointF(6, 9)])
        painter.drawPolygon(polygon)

    return _save_icon(f"triangle-{direction}-{color.lstrip('#')}", draw)


def _check_icon(color: str) -> str:
    """核取方塊打勾用的勾號——不能只靠 ``background-color`` 分辨勾沒勾。"""

    def draw(painter: QPainter) -> None:
        pen = painter.pen()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        # 用兩段粗線疊出勾號的形狀（QSS 沒有 stroke-based path，用多邊形代替）。
        polygon = QPolygonF(
            [
                QPointF(2.2, 6.2),
                QPointF(4.6, 8.8),
                QPointF(9.6, 3.2),
                QPointF(10.6, 4.1),
                QPointF(4.7, 10.6),
                QPointF(1.2, 7.1),
            ]
        )
        painter.drawPolygon(polygon)
        painter.setPen(pen)

    return _save_icon(f"check-{color.lstrip('#')}", draw)


def _dash_icon(color: str) -> str:
    """``QCheckBox`` 半選（``:indeterminate``）狀態用的橫槓，跟打勾/空白都要分得出來。"""

    def draw(painter: QPainter) -> None:
        painter.setBrush(QColor(color))
        painter.drawRect(2, 5, 8, 2)

    return _save_icon(f"dash-{color.lstrip('#')}", draw)


def _radio_dot_icon(color: str) -> str:
    """單選鈕選取狀態用的實心圓點（畫在圈圈中間，不是整圈填滿）。"""

    def draw(painter: QPainter) -> None:
        painter.setBrush(QColor(color))
        painter.drawEllipse(QPointF(_ICON_SIZE / 2, _ICON_SIZE / 2), 3, 3)

    return _save_icon(f"radio-{color.lstrip('#')}", draw)


def resolve_mode(theme_setting: str) -> str:
    """把 config 的 system/light/dark 換成實際要用的 ``"light"`` 或 ``"dark"``。"""
    if theme_setting in ("light", "dark"):
        return theme_setting

    # "system"：問作業系統。任何理由問不到就當作亮色，不要讓介面打不開。
    try:
        scheme = QGuiApplication.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return "dark"
    except Exception:  # pragma: no cover - 舊版 Qt 或平台不支援時的保險
        pass
    return "light"


def _pick(pair: tuple[str, str], mode: str) -> str:
    return pair[1] if mode == "dark" else pair[0]


def pick(pair: tuple[str, str]) -> str:
    """用目前套用的主題模式，把一組 (亮, 暗) 顏色解析成單一顏色字串。

    給需要「一個顏色值」而非整份 QSS 的地方用，例如 :class:`~gui_qt.widgets.StatusBar`
    依訊息語氣（成功／錯誤／靜音）換字色。
    """
    return _pick(pair, _current_mode)


def current_mode() -> str:
    """目前套用的主題模式：``"light"`` 或 ``"dark"``。"""
    return _current_mode


def stylesheet(mode: str) -> str:
    """整個應用程式的 QSS，一次套在 ``QApplication`` 上。

    ## 「每個字後面一塊白框」的根因與修法

    舊版第一條規則是 ``QMainWindow, QWidget {{ background-color: ...; }}``。
    Qt 的型別選擇器是照*類別*比對，``QWidget`` 會比對到程式裡*所有*
    widget——``QLabel``、``QLineEdit``、``QCheckBox``…全部都是 ``QWidget``
    的子類別，所以每一個都被直接套上不透明的 ``window_bg``。標籤常常放在
    ``card_fg`` 色的卡片／側邊欄上，結果就是每個字底下都浮出一塊視窗底色
    的方框——使用者形容的「白色框框」、「一白一灰」都是同一個成因。

    另外，Qt 只要偵測到「有規則直接命中某個 widget 的 background/border
    等屬性」，那個 widget 就會整個改由 QSS 的 box-model 引擎接手繪製，
    不再呼叫原生樣式（``windowsvista``）畫那些做了視覺主題的複雜零件
    （凹陷外框、核取方塊、下拉箭頭…）。這正是使用者截圖裡「``QLineEdit``
    只剩一條底線、核取方塊打勾後方框整個消失」的原因：舊版的
    ``QWidget`` 規則命中了它們，原生外觀被拿掉了一部分，卻沒有補一套
    完整的樣式頂上去，變成「半殘」的樣子。

    修法呼應 Qt 的慣例：

    1. 視窗底色改由 ``QMainWindow`` 專屬選擇器負責，不再用裸的
       ``QWidget`` 設 background。
    2. ``QWidget {{ color: ... }}`` 保留（純文字顏色可以安全地廣播，不會
       觸發上面那個「原生外觀被拿掉」的問題），``QLabel`` 另外明確設
       ``background: transparent``，讓標籤永遠透出所在容器（卡片/側邊欄/
       頁面）的顏色，不會自己疊一塊色塊。
    3. 既然任何輸入類/按鈕類控制項只要出現在這份 QSS 裡就會變成「Qt 自己
       畫」，那就乾脆把它們畫完整：``QLineEdit``/``QComboBox``/
       ``QCheckBox``/``QPushButton``/``QScrollBar`` 全部給一套完整、四態
       （一般/hover/pressed/disabled）都有定義的樣式，而不是只設一半、
       讓原生樣式補另一半——那正是舊版「殘缺外觀」的來源。
    """
    window_bg = _pick(WINDOW_BG, mode)
    text_fg = _pick(TEXT_FG, mode)
    card_fg = _pick(CARD_FG, mode)
    muted = _pick(MUTED, mode)
    accent = _pick(ACCENT, mode)
    accent_hover = _pick(ACCENT_HOVER, mode)
    accent_pressed = _pick(ACCENT_PRESSED, mode)
    danger = _pick(DANGER, mode)
    danger_pressed = _pick(DANGER_PRESSED, mode)
    hover = _pick(HOVER, mode)
    border = _pick(BORDER, mode)
    stripe = _pick(TABLE_STRIPE, mode)
    scrollbar = _pick(SCROLLBAR_HANDLE, mode)
    scrollbar_hover = _pick(SCROLLBAR_HANDLE_HOVER, mode)

    # 下拉箭頭／增減鍵頭一律用 muted 色（次要視覺元素，不用跟文字一樣搶眼）；
    # 打勾/橫槓疊在 accent 色塊上，一律用白色才有足夠對比。
    down_arrow_icon = _triangle_icon(muted, "down")
    up_arrow_icon = _triangle_icon(muted, "up")
    check_icon = _check_icon("#ffffff")
    dash_icon = _dash_icon("#ffffff")
    # 單選鈕選取狀態沒有把整個 indicator 填色（維持 window_bg 的圈），
    # 圓點本身要用 accent 色才會跟底色有對比，不能像打勾圖示一樣用白色。
    radio_dot_icon = _radio_dot_icon(accent)

    return f"""
    QMainWindow {{
        background-color: {window_bg};
    }}
    QWidget {{
        color: {text_fg};
    }}
    QLabel {{
        background: transparent;
    }}
    QScrollArea {{
        background: transparent;
        border: none;
    }}
    QScrollArea > QWidget, QScrollArea > QWidget > QWidget {{
        background: transparent;
    }}
    QToolTip {{
        background-color: {card_fg};
        color: {text_fg};
        border: 1px solid {border};
        padding: 4px 6px;
    }}
    QWidget#Sidebar {{
        background-color: {card_fg};
        border-right: 1px solid {border};
    }}
    QLabel#MutedLabel {{
        color: {muted};
        background: transparent;
    }}
    QPushButton#NavButton {{
        text-align: left;
        padding: 0 12px;
        border: none;
        border-radius: 8px;
        background: transparent;
        color: {text_fg};
    }}
    QPushButton#NavButton:hover {{
        background-color: {hover};
    }}
    QPushButton#NavButton:checked {{
        background-color: {accent};
        color: #ffffff;
    }}
    QFrame#Section, QFrame#StatCard {{
        background-color: {card_fg};
        border-radius: 10px;
        border: 1px solid {border};
    }}
    QLabel#SectionTitle {{
        font-weight: bold;
        font-size: 15px;
        padding-bottom: 2px;
        background: transparent;
    }}

    /* ---------------------------------------------------- 輸入類控制項
       QLineEdit/QComboBox/QSpinBox 統一給完整外框、內距、焦點色，不要
       一個有滿框一個只剩底線。 */
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
        background-color: {window_bg};
        color: {text_fg};
        border: 1px solid {border};
        border-radius: 6px;
        padding: 4px 8px;
        min-height: 22px;
        selection-background-color: {accent};
        selection-color: #ffffff;
    }}
    QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{
        border-color: {accent};
    }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
        border: 1px solid {accent};
    }}
    QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
        color: {muted};
        background-color: {card_fg};
        border-color: {border};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 22px;
    }}
    QComboBox::down-arrow {{
        image: url({down_arrow_icon});
        width: 10px;
        height: 10px;
        margin-right: 8px;
    }}
    QComboBox QAbstractItemView {{
        background-color: {card_fg};
        color: {text_fg};
        border: 1px solid {border};
        outline: none;
        selection-background-color: {accent};
        selection-color: #ffffff;
    }}

    /* ---------------------------------------------------- QSpinBox 增減鍵頭
       跟下拉箭頭同一種成因/同一種修法：三角形一律用預先畫好的 PNG 圖示，
       不要用 border 三角形技巧（親測在這個 Qt 版本不會畫出三角形）。 */
    QSpinBox::up-button, QDoubleSpinBox::up-button {{
        subcontrol-origin: border;
        subcontrol-position: top right;
        width: 18px;
        border-left: 1px solid {border};
        background: transparent;
    }}
    QSpinBox::down-button, QDoubleSpinBox::down-button {{
        subcontrol-origin: border;
        subcontrol-position: bottom right;
        width: 18px;
        border-left: 1px solid {border};
        background: transparent;
    }}
    QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
    QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
        background-color: {hover};
    }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
        image: url({up_arrow_icon});
        width: 8px;
        height: 8px;
    }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
        image: url({down_arrow_icon});
        width: 8px;
        height: 8px;
    }}

    /* ---------------------------------------------------- 核取/選項方塊
       checked 狀態除了填 accent 色塊，還要疊一個白色打勾圖示——光靠顏色分
       勾沒勾不夠明顯（勾選清單裡一排藍色方塊會分不出誰打勾誰沒打勾）。
       半選（``:indeterminate``）用橫槓，跟打勾/空白三態都分得出來；單選鈕
       用圈內小圓點（不是整圈填色），符合單選鈕的慣用視覺語言。 */
    QCheckBox, QRadioButton {{
        spacing: 8px;
        background: transparent;
    }}
    QCheckBox::indicator, QRadioButton::indicator {{
        width: 16px;
        height: 16px;
        border: 1px solid {border};
        border-radius: 4px;
        background-color: {window_bg};
    }}
    QRadioButton::indicator {{
        border-radius: 8px;
    }}
    QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
        border-color: {accent};
    }}
    QCheckBox::indicator:checked {{
        background-color: {accent};
        border-color: {accent};
        image: url({check_icon});
    }}
    QCheckBox::indicator:indeterminate {{
        background-color: {accent};
        border-color: {accent};
        image: url({dash_icon});
    }}
    QRadioButton::indicator:checked {{
        border-color: {accent};
        image: url({radio_dot_icon});
    }}
    QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
        background-color: {card_fg};
        border-color: {border};
    }}
    QCheckBox:disabled, QRadioButton:disabled {{
        color: {muted};
    }}

    /* ---------------------------------------------------- 按鈕
       預設是「次要」樣式（淡底＋外框）。頁面之後可以把主要動作按鈕
       setObjectName("PrimaryButton")、危險動作 setObjectName("DangerButton")
       來取用下面兩組強調樣式——目前沒有頁面這樣設，所以看到的都還是
       次要樣式，這兩組規則先準備好、等頁面接上就會生效。 */
    QPushButton {{
        background-color: {card_fg};
        color: {text_fg};
        border: 1px solid {border};
        border-radius: 6px;
        padding: 6px 14px;
        min-height: 22px;
    }}
    QPushButton:hover {{
        background-color: {hover};
        border-color: {accent};
    }}
    QPushButton:pressed {{
        background-color: {hover};
        border-color: {accent_pressed};
    }}
    QPushButton:disabled {{
        color: {muted};
        background-color: {card_fg};
        border-color: {border};
    }}
    QPushButton#NavButton:disabled {{
        color: {muted};
        background: transparent;
        border: none;
    }}
    QPushButton#PrimaryButton {{
        background-color: {accent};
        color: #ffffff;
        border: 1px solid {accent};
        font-weight: bold;
    }}
    QPushButton#PrimaryButton:hover {{
        background-color: {accent_hover};
        border-color: {accent_hover};
    }}
    QPushButton#PrimaryButton:pressed {{
        background-color: {accent_pressed};
        border-color: {accent_pressed};
    }}
    QPushButton#PrimaryButton:disabled {{
        background-color: {border};
        color: {muted};
        border-color: {border};
    }}
    QPushButton#DangerButton {{
        background-color: transparent;
        color: {danger};
        border: 1px solid {danger};
    }}
    QPushButton#DangerButton:hover {{
        background-color: {danger};
        color: #ffffff;
    }}
    QPushButton#DangerButton:pressed {{
        background-color: {danger_pressed};
        color: #ffffff;
        border-color: {danger_pressed};
    }}
    QPushButton#DangerButton:disabled {{
        color: {muted};
        border-color: {border};
        background-color: transparent;
    }}

    /* ---------------------------------------------------- 表格 */
    QTableView {{
        background-color: {window_bg};
        alternate-background-color: {stripe};
        gridline-color: {border};
        selection-background-color: {accent};
        selection-color: #ffffff;
        border: 1px solid {border};
        border-radius: 6px;
    }}
    QHeaderView::section {{
        background-color: {card_fg};
        color: {text_fg};
        padding: 6px 10px;
        border: none;
        border-bottom: 1px solid {border};
        font-weight: bold;
    }}

    /* ---------------------------------------------------- 文字輸出／清單類控制項
       這些跟 QTableView 一樣是 QAbstractScrollArea 的子類別：viewport 預設是
       QPalette::Base（不受任何 QWidget 規則影響，永遠是白色），拿掉舊版
       QWidget 廣播規則之後如果不明確指定，日誌／活動紀錄這類 QTextEdit、
       QListWidget 就會在暗色主題下變成刺眼的白色方塊——跟本檔案開頭說明的
       「QLineEdit 只剩底線」是同一種成因，這裡一併補齊。 */
    QPlainTextEdit, QTextEdit {{
        background-color: {window_bg};
        color: {text_fg};
        border: 1px solid {border};
        border-radius: 6px;
        selection-background-color: {accent};
        selection-color: #ffffff;
    }}
    QPlainTextEdit:disabled, QTextEdit:disabled {{
        color: {muted};
        background-color: {card_fg};
    }}
    QListWidget, QTreeWidget, QListView, QTreeView {{
        background-color: {window_bg};
        color: {text_fg};
        border: 1px solid {border};
        border-radius: 6px;
        alternate-background-color: {stripe};
        selection-background-color: {accent};
        selection-color: #ffffff;
        outline: none;
    }}
    QTreeView::item, QListView::item {{
        padding: 2px 4px;
    }}
    QTreeView::indicator, QListView::indicator {{
        width: 16px;
        height: 16px;
        border: 1px solid {border};
        border-radius: 4px;
        background-color: {window_bg};
    }}
    QTreeView::indicator:checked, QListView::indicator:checked {{
        background-color: {accent};
        border-color: {accent};
        image: url({check_icon});
    }}
    QWidget#StatusBar {{
        background-color: {card_fg};
        border-top: 1px solid {border};
    }}

    /* ---------------------------------------------------- 捲軸
       扁平樣式：隱藏上下/左右箭頭、軌道透明、滑塊圓角，寬度統一 11px。 */
    QScrollBar:vertical {{
        background: transparent;
        width: 11px;
        margin: 2px 2px 2px 0;
    }}
    QScrollBar::handle:vertical {{
        background: {scrollbar};
        min-height: 30px;
        border-radius: 5px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {scrollbar_hover};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0px;
        border: none;
        background: none;
    }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        background: none;
    }}
    QScrollBar:horizontal {{
        background: transparent;
        height: 11px;
        margin: 0 2px 2px 2px;
    }}
    QScrollBar::handle:horizontal {{
        background: {scrollbar};
        min-width: 30px;
        border-radius: 5px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: {scrollbar_hover};
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0px;
        border: none;
        background: none;
    }}
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
        background: none;
    }}
    """


def apply_theme(app: QApplication, theme_setting: str) -> str:
    """套用主題，回傳實際使用的模式，方便呼叫端記錄／顯示除錯訊息。"""
    global _current_mode
    mode = resolve_mode(theme_setting)
    _current_mode = mode
    app.setStyleSheet(stylesheet(mode))
    return mode
