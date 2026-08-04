"""控制器層——介面與資料之間唯一的接縫。

頁面不開 session、不組查詢、不 import repository，一律呼叫這裡的控制器，
拿回純資料（DTO）。當初就是因為有這一層，整個介面從 customtkinter 換成
PySide6 時後端一行都不用動。

原本住在 `gui/controllers*.py`，隨著舊 Tk 介面下線搬到這裡——它們跟任何
特定的介面框架都沒有關係。
"""

from controllers.core import (
    CompanyController,
    ContactController,
    CrawlController,
    DashboardController,
    EnrichController,
    ExportController,
    ImportController,
    LogController,
    SettingsController,
    VerifyController,
)
from controllers.mail import MailController
from controllers.source import SourceWizardController

__all__ = [
    "CompanyController",
    "ContactController",
    "CrawlController",
    "DashboardController",
    "EnrichController",
    "ExportController",
    "ImportController",
    "LogController",
    "MailController",
    "SettingsController",
    "SourceWizardController",
    "VerifyController",
]
