"""Gmail 標籤的控制器：網頁版與桌面版共用。

每個會連 Gmail 的動作都自己開一條連線、做完就關。讀的動作跟寫的動作分開：
「預覽」一律唯讀，使用者看過按下去才寫。見 :mod:`gmail.labels`。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import Any

from core.config import AppConfig, get_config
from core.data_version import bump
from core.errors import CRMError
from gmail.client import GmailClient
from gmail.mailbox import GmailLabel, encode_mailbox
from gmail.labels import LabelScan, SyncResult, TagMatch, apply_tags, scan_label, sync_labels


class GmailLabelController:
    def __init__(
        self,
        config: AppConfig | None = None,
        client_factory: Callable[[AppConfig], GmailClient] = GmailClient,
    ) -> None:
        self.config = config or get_config()
        #: 測試換成假的 IMAP。
        self._client_factory = client_factory

    @contextmanager
    def _client(self) -> Iterator[GmailClient]:
        client = self._client_factory(self.config)
        client.connect()
        try:
            yield client
        finally:
            client.close()

    def labels(self, *, report: Any = None, cancel_event: Any = None) -> list[str]:
        """使用者自己建的 Gmail 標籤名稱，照字母排。"""
        with self._client() as client:
            return sorted(label.name for label in client.list_labels() if label.is_user_label)

    # ------------------------------------------------------ 讀進來：分類

    def scan_classify(
        self, label: str, limit: int, *, report: Any = None, cancel_event: Any = None
    ) -> LabelScan:
        """這個 Gmail 標籤會替名單裡哪些公司貼上同名標籤。唯讀。"""
        with self._client() as client:
            return scan_label(
                client, _label(label), limit, self.config.gmail.address, report=report
            )

    def apply_classify(
        self, matches: Iterable[TagMatch], *, report: Any = None, cancel_event: Any = None
    ) -> int:
        added = apply_tags(matches)
        bump()
        return added

    # ------------------------------------------------------ 讀進來：挖新公司

    def scan_harvest(
        self, label: str, limit: int, *, report: Any = None, cancel_event: Any = None
    ):
        """從這個 Gmail 標籤的信件簽名檔挖公司。唯讀。"""
        from gmail.harvester import scan_for_contacts

        with self._client() as client:
            client.open_mailbox(encode_mailbox(label))
            try:
                return scan_for_contacts(
                    "ALL", limit, self.config, client, report=report, cancel_event=cancel_event
                )
            except CRMError:
                raise
            except Exception as exc:
                raise CRMError(f"讀「{label}」時出錯：{exc}") from exc

    def apply_harvest(
        self, candidates, label: str, *, report: Any = None, cancel_event: Any = None
    ):
        """把勾起來的存進名單，每一家都貼上 ``label``。"""
        from gmail.harvester import store_candidates

        summary = store_candidates(
            candidates, self.config, tag=label, report=report, cancel_event=cancel_event
        )
        bump()
        return summary

    # ------------------------------------------------------ 寫回去

    def sync(
        self,
        include_tags: bool,
        include_status: bool,
        apply: bool,
        *,
        report: Any = None,
        cancel_event: Any = None,
    ) -> SyncResult:
        """把名單匠的分類貼回 Gmail。``apply=False`` 是預覽。"""
        with self._client() as client:
            return sync_labels(
                client,
                include_tags=include_tags,
                include_status=include_status,
                apply=apply,
                report=report,
                cancel_event=cancel_event,
            )


def _label(name: str) -> GmailLabel:
    """畫面上選的標籤名稱 -> 開信箱要用的那一份。"""
    return GmailLabel(name=name, mailbox=encode_mailbox(name))
