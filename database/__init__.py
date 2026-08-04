"""Persistence: ORM entities, session management, repositories, backups."""

from database.models import (
    Activity,
    Attachment,
    Base,
    Company,
    Contact,
    CrawlJob,
    MXCache,
    Tag,
)
from database.repository import (
    ActivityRepository,
    AttachmentRepository,
    CompanyRepository,
    ContactRepository,
    CrawlJobRepository,
    MXCacheRepository,
    StatsRepository,
    TagRepository,
)
from database.session import get_engine, init_db, reset_engine, session_scope

__all__ = [
    "Activity",
    "ActivityRepository",
    "Attachment",
    "AttachmentRepository",
    "Base",
    "Company",
    "CompanyRepository",
    "Contact",
    "ContactRepository",
    "CrawlJob",
    "CrawlJobRepository",
    "MXCache",
    "MXCacheRepository",
    "StatsRepository",
    "Tag",
    "TagRepository",
    "get_engine",
    "init_db",
    "reset_engine",
    "session_scope",
]
