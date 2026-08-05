"""介面顯示用的中英對照。

資料庫存的仍然是 :mod:`core.constants` 裡的英文值，匯出檔案也一樣——
這樣既有資料、CLI 與匯出格式都不會因為介面語言而改變。
中文只在「顯示」這一層出現，畫面上選了中文標籤之後，
一律用 :func:`to_value` 轉回英文值再交給 controller。

    stage_menu.configure(values=stage_labels())
    controller.set_stage(company_id, to_value(stage_menu.get()))
"""

from __future__ import annotations

from core.constants import (
    ActivityType,
    CrawlStatus,
    EmailVerdict,
    LogCategory,
    PipelineStage,
    Priority,
    RecordStatus,
)

#: 業務階段
STAGE_LABELS: dict[str, str] = {
    PipelineStage.NEW.value: "新名單",
    PipelineStage.QUALIFIED.value: "已評估",
    PipelineStage.CONTACTED.value: "已聯絡",
    PipelineStage.MEETING.value: "已會面",
    PipelineStage.PROPOSAL.value: "已報價",
    PipelineStage.NEGOTIATION.value: "議價中",
    PipelineStage.WON.value: "已成交",
    PipelineStage.LOST.value: "已失單",
    PipelineStage.INACTIVE.value: "暫不往來",
}

#: 紀錄狀態（與業務階段無關）
STATUS_LABELS: dict[str, str] = {
    RecordStatus.ACTIVE.value: "使用中",
    RecordStatus.DUPLICATE.value: "重複",
    RecordStatus.INVALID.value: "無效",
    RecordStatus.ARCHIVED.value: "已封存",
}

#: 優先度
PRIORITY_LABELS: dict[str, str] = {
    Priority.LOW.value: "低",
    Priority.MEDIUM.value: "中",
    Priority.HIGH.value: "高",
    Priority.URGENT.value: "緊急",
}

#: 活動類型
ACTIVITY_LABELS: dict[str, str] = {
    ActivityType.NOTE.value: "備註",
    ActivityType.CALL.value: "電話",
    ActivityType.EMAIL.value: "郵件",
    ActivityType.MEETING.value: "會議",
    ActivityType.STAGE_CHANGE.value: "階段變更",
    ActivityType.SYSTEM.value: "系統",
}

#: Email 驗證結果
VERDICT_LABELS: dict[str, str] = {
    EmailVerdict.UNKNOWN.value: "未檢查",
    EmailVerdict.EMPTY.value: "無信箱",
    EmailVerdict.INVALID_SYNTAX.value: "格式錯誤",
    EmailVerdict.DISPOSABLE.value: "拋棄式信箱",
    EmailVerdict.NO_MX.value: "查無 MX",
    EmailVerdict.VALID.value: "有效",
}

#: 爬取狀態
CRAWL_STATUS_LABELS: dict[str, str] = {
    CrawlStatus.PENDING.value: "等待中",
    CrawlStatus.RUNNING.value: "執行中",
    CrawlStatus.SUCCESS.value: "成功",
    CrawlStatus.PARTIAL.value: "部分成功",
    CrawlStatus.FAILED.value: "失敗",
    CrawlStatus.CANCELLED.value: "已取消",
}

#: 日誌分類
LOG_LABELS: dict[str, str] = {
    LogCategory.CRAWL.value: "爬取",
    LogCategory.DATABASE.value: "資料庫",
    LogCategory.EXPORT.value: "匯出",
    LogCategory.GUI.value: "介面",
    LogCategory.ERROR.value: "錯誤",
}

#: 匯出格式
FORMAT_LABELS: dict[str, str] = {
    "excel": "Excel 活頁簿 (.xlsx)",
    "csv": "CSV 逗號分隔 (.csv)",
    "json": "JSON (.json)",
}

#: 匯出／表格欄位
FIELD_LABELS: dict[str, str] = {
    "id": "編號",
    "company_name": "公司名稱",
    "tax_id": "統一編號",
    "email": "電子信箱",
    "phone": "電話",
    "website": "網站",
    "address": "地址",
    "industry": "產業",
    "english_name": "英文名稱",
    "fax": "傳真",
    "products": "主要產品",
    "contact_person": "聯絡人",
    "pipeline_stage": "業務階段",
    "priority": "優先度",
    "status": "狀態",
    "email_verdict": "信箱驗證",
    "tags": "標籤",
    "source": "資料來源",
    "source_url": "來源網址",
    "follow_up_date": "追蹤日期",
    "created_at": "建立時間",
    "updated_at": "更新時間",
    "remark": "備註",
    "name": "姓名",
    "title": "職稱",
    "mobile": "手機",
    "is_primary": "主要聯絡人",
    "company_id": "公司編號",
    "type": "類型",
    "subject": "主旨",
    "body": "內容",
    "occurred_at": "發生時間",
    "filename": "檔名",
    "size_bytes": "大小",
    "uploaded_at": "上傳時間",
}

#: 「全部」選項，用在各種篩選下拉選單
ALL_OPTION = "全部"

_ALL_MAPS = (
    STAGE_LABELS,
    STATUS_LABELS,
    PRIORITY_LABELS,
    ACTIVITY_LABELS,
    VERDICT_LABELS,
    CRAWL_STATUS_LABELS,
    LOG_LABELS,
    FORMAT_LABELS,
)

# 反查表：中文標籤 -> 英文值。標籤唯一，所以一張表就夠。
_REVERSE: dict[str, str] = {
    label: value for mapping in _ALL_MAPS for value, label in mapping.items()
}


def label(value: str | None, mapping: dict[str, str] | None = None) -> str:
    """把儲存值轉成顯示用的中文；查不到就原樣回傳。

    查不到時回傳原值而不是丟例外——多一個沒翻到的字串，
    總比整個畫面因為一個未知值而掛掉好。
    """
    if value is None:
        return ""
    if mapping is not None:
        return mapping.get(value, value)
    for candidate in _ALL_MAPS:
        if value in candidate:
            return candidate[value]
    return value


def to_value(text: str | None, mapping: dict[str, str] | None = None) -> str:
    """把畫面上選到的中文標籤轉回儲存值。"""
    if not text or text == ALL_OPTION:
        return ""
    if mapping is not None:
        for value, shown in mapping.items():
            if shown == text:
                return value
        return text
    return _REVERSE.get(text, text)


def labels(mapping: dict[str, str], with_all: bool = False) -> list[str]:
    """下拉選單用的中文選項清單。"""
    options = list(mapping.values())
    return [ALL_OPTION, *options] if with_all else options


def stage_labels(with_all: bool = False) -> list[str]:
    return labels(STAGE_LABELS, with_all)


def status_labels(with_all: bool = False) -> list[str]:
    return labels(STATUS_LABELS, with_all)


def priority_labels(with_all: bool = False) -> list[str]:
    return labels(PRIORITY_LABELS, with_all)


def activity_labels() -> list[str]:
    return labels(ACTIVITY_LABELS)


def field_label(name: str) -> str:
    """欄位代碼轉中文標題。"""
    return FIELD_LABELS.get(name, name)
