"""SQLAlchemy ORM entities.

Time convention: every timestamp is a **naive local-time** ``datetime`` produced
by :func:`now`. This is a single-user desktop application, so storing local time
keeps "new today" queries and exported timestamps directly meaningful without a
conversion layer. Do not mix in ``datetime.utcnow()``.

Encryption: columns holding **personal data** use
:class:`~database.types.EncryptedString` / :class:`~database.types.EncryptedText`,
which encrypt on write and decrypt on read. Business identifiers (company name,
tax id, industry, website) stay in clear so SQL can still search and sort them.
See :mod:`database.types` for why the line is drawn there.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from core.constants import (
    ActivityType,
    CrawlStatus,
    EmailStatus,
    EmailVerdict,
    PipelineStage,
    Priority,
    RecordStatus,
)
from database.types import EncryptedJson, EncryptedString, EncryptedText


def now() -> datetime:
    """Current local time, naive. The single clock for the whole schema."""
    return datetime.now()


class Base(DeclarativeBase):
    """Declarative base for every entity."""


company_tags = Table(
    "company_tags",
    Base.metadata,
    Column("company_id", ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Company(Base):
    """A B2B company record.

    ``dedupe_key`` is the strongest identity signal available for the record
    (tax id > email > name+phone > name+website > name). It is unique, and the
    repository upserts against it -- that is how duplicate detection works. It
    is encrypted because a ``mail:`` key embeds an address; deterministic
    encryption keeps the unique index and the upsert lookup working on it.
    """

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # --- identity ---
    company_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name_key: Mapped[str] = mapped_column(String(255), default="", index=True)
    dedupe_key: Mapped[str] = mapped_column(
        EncryptedString(320), default="", unique=True, index=True
    )
    tax_id: Mapped[str | None] = mapped_column(String(16), index=True)

    # --- contact ---
    email: Mapped[str | None] = mapped_column(
        EncryptedString(320, lowercase=True), index=True
    )
    phone: Mapped[str | None] = mapped_column(EncryptedString(64), index=True)
    website: Mapped[str | None] = mapped_column(String(512))
    address: Mapped[str | None] = mapped_column(EncryptedString(512))
    industry: Mapped[str | None] = mapped_column(String(128), index=True)
    #: 英文名稱。台灣的公協會名錄幾乎都會列，做外銷或對帳時要用。
    #: 跟中文名一樣是商業識別資訊、不是個資，所以不加密——加密的欄位
    #: 沒辦法用 SQL 搜尋與排序。
    english_name: Mapped[str | None] = mapped_column(String(255), index=True)
    #: 傳真。跟電話同一類，是個資（會連到特定辦公室），所以加密。
    fax: Mapped[str | None] = mapped_column(EncryptedString(64))
    #: 主要產品／代理品項。名錄上常見的「代理廠商及代銷產品」。
    products: Mapped[str | None] = mapped_column(Text)
    contact_person: Mapped[str | None] = mapped_column(EncryptedString(128))

    # --- 公司登記（經濟部商業司開放資料，靠統一編號查回來的） ---
    #
    # 這三欄不放進 extra_fields，因為它們要被排序與篩選——名單品質分數會看
    # 資本額，而「這家公司還在不在」是使用者寄信之前唯一真的想知道的事。
    # extra_fields 是加密的 JSON，SQL 進不去。這些是公開的商業登記資訊、
    # 不是個資，跟公司名稱、統編同一類，所以也不加密。
    #: 登記資本額（新台幣元）。實收資本額另外放在 extra_fields 裡。
    capital_amount: Mapped[int | None] = mapped_column(Integer, index=True)
    #: 「核准設立」「解散」「撤銷」「廢止」……原文照存。
    registration_status: Mapped[str | None] = mapped_column(String(32), index=True)
    #: 最後一次去查登記資料的時間。有值就代表查過了——查回來是「查無此統編」
    #: 的也算查過，不然每一次補完都會把同一批查不到的再查一遍。
    registration_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    #: 這個名錄有、但程式沒有對應欄位的東西，原樣保留。
    #:
    #: 每個公會列的欄位都不一樣——旅行公會有「會員代表」「入會年月日」
    #: 「註冊編號」，化工公會有「代理廠商及代銷產品」。要嘛替每一種名錄加一個
    #: 欄位（加不完，而且對其他名錄全是空的），要嘛丟掉（使用者實際看到的東西
    #: 就這樣消失了）。存成 key/value 是第三條路：抓到什麼就存什麼，在詳細資料
    #: 裡照原本的標籤顯示。
    #:
    #: 代價是它不能用 SQL 搜尋或排序，所以只有「已知欄位裝不下」的東西才放這裡。
    extra_fields: Mapped[dict[str, str]] = mapped_column(
        EncryptedJson, default=dict, nullable=True
    )

    # --- provenance ---
    source: Mapped[str | None] = mapped_column(String(128), index=True)
    source_url: Mapped[str | None] = mapped_column(String(1024))

    # --- CRM ---
    status: Mapped[str] = mapped_column(
        String(32), default=RecordStatus.ACTIVE.value, index=True
    )
    pipeline_stage: Mapped[str] = mapped_column(
        String(32), default=PipelineStage.NEW.value, index=True
    )
    priority: Mapped[str] = mapped_column(String(16), default=Priority.MEDIUM.value)
    follow_up_date: Mapped[date | None] = mapped_column(Date, index=True)
    remark: Mapped[str | None] = mapped_column(EncryptedText)

    # --- verification ---
    email_verdict: Mapped[str] = mapped_column(
        String(32), default=EmailVerdict.UNKNOWN.value, index=True
    )
    email_checked_at: Mapped[datetime | None] = mapped_column(DateTime)

    # --- outreach ---
    #: Suppression flag. Set when someone asks not to be contacted again; the
    #: sender skips these unconditionally and it is never cleared by a crawl.
    do_not_contact: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    last_emailed_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    email_count: Mapped[int] = mapped_column(Integer, default=0)

    # --- audit ---
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    # Tags are eager: every row of the company list shows them, so loading them
    # in one extra query beats a lazy load per row.
    tags: Mapped[list["Tag"]] = relationship(
        secondary=company_tags, back_populates="companies", lazy="selectin"
    )
    # These three are not. Only the detail page reads them, and it reads one
    # company at a time -- eager-loading them made every listing of 216
    # companies drag along their contacts, history and attachments unread.
    # Every access sits inside a session, so a lazy load is safe here.
    contacts: Mapped[list["Contact"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    activities: Mapped[list["Activity"]] = relationship(
        back_populates="company",
        cascade="all, delete-orphan",
        order_by="Activity.occurred_at.desc()",
    )
    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_companies_stage_status", "pipeline_stage", "status"),
        Index("ix_companies_name_phone", "name_key", "phone"),
    )

    def tag_names(self) -> list[str]:
        return sorted(tag.name for tag in self.tags)

    def __repr__(self) -> str:
        return f"<Company id={self.id} name={self.company_name!r}>"


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    color: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    companies: Mapped[list[Company]] = relationship(
        secondary=company_tags, back_populates="tags"
    )

    def __repr__(self) -> str:
        return f"<Tag {self.name!r}>"


class Contact(Base):
    """A named person at a company."""

    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(EncryptedString(128), nullable=False)
    title: Mapped[str | None] = mapped_column(String(128))
    email: Mapped[str | None] = mapped_column(
        EncryptedString(320, lowercase=True), index=True
    )
    phone: Mapped[str | None] = mapped_column(EncryptedString(64))
    mobile: Mapped[str | None] = mapped_column(EncryptedString(64))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    remark: Mapped[str | None] = mapped_column(EncryptedText)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    company: Mapped[Company] = relationship(back_populates="contacts")

    __table_args__ = (
        UniqueConstraint("company_id", "name", "email", name="uq_contact_identity"),
    )

    def __repr__(self) -> str:
        return f"<Contact id={self.id} name={self.name!r}>"


class Activity(Base):
    """One entry of a company's activity history."""

    __tablename__ = "activities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(32), default=ActivityType.NOTE.value)
    # The subject is a short system label ("new -> contacted"); the body is
    # where a user writes about a person, so only the body is encrypted.
    subject: Mapped[str | None] = mapped_column(String(255))
    body: Mapped[str | None] = mapped_column(EncryptedText)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    company: Mapped[Company] = relationship(back_populates="activities")

    def __repr__(self) -> str:
        return f"<Activity id={self.id} type={self.type!r}>"


class Attachment(Base):
    """A file attached to a company, copied into the app's data directory."""

    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    company: Mapped[Company] = relationship(back_populates="attachments")

    def __repr__(self) -> str:
        return f"<Attachment id={self.id} filename={self.filename!r}>"


class MailAttachment(Base):
    """一個可以隨信寄出的檔案。使用者自己的附件庫。

    跟上面的 :class:`Attachment` 不同：那個是「掛在某一家公司底下的檔案」，
    這個是「可以附加到任何一封開發信的檔案」，兩者沒有關係。

    **檔案內容不存在資料庫裡**，只存在 ``attachments/`` 資料夾。單一附件可以
    到 20MB，塞進 SQLite 會讓資料庫肥到幾百 MB、每次備份都要複製一遍，而備份
    是這支程式預設就會做的事。這裡存的是索引與後設資料：顯示名稱、備註、
    加入時間、用過幾次。

    檔案是否還在，一律在讀取時去問檔案系統（見 ``exists``），不存成欄位——
    使用者可以直接開資料夾把檔案拖走，存成欄位只會得到一個過期的答案。
    """

    __tablename__ = "mail_attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: ``attachments/`` 底下的實際檔名。同一個檔名只會有一筆。
    filename: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    #: 顯示用的名稱。改這個不會動到檔案，所以使用者可以把
    #: "2026Q1_catalog_final_v3.pdf" 顯示成「2026 春季型錄」。
    label: Mapped[str] = mapped_column(String(255), default="")
    mime_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    #: 使用者自己的備註，例如「舊版，別再寄了」。
    note: Mapped[str | None] = mapped_column(Text)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    #: 最後一次真的被寄出去的時間與累計次數。用來判斷哪些附件可以清掉。
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    use_count: Mapped[int] = mapped_column(Integer, default=0)

    @property
    def display_name(self) -> str:
        return self.label.strip() or self.filename

    def __repr__(self) -> str:
        return f"<MailAttachment id={self.id} filename={self.filename!r}>"


class CrawlJob(Base):
    """History of crawl runs; powers the Dashboard's crawl status panel."""

    __tablename__ = "crawl_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), default=CrawlStatus.PENDING.value)
    pages_crawled: Mapped[int] = mapped_column(Integer, default=0)
    records_found: Mapped[int] = mapped_column(Integer, default=0)
    records_new: Mapped[int] = mapped_column(Integer, default=0)
    records_updated: Mapped[int] = mapped_column(Integer, default=0)
    records_duplicate: Mapped[int] = mapped_column(Integer, default=0)
    records_invalid: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    #: 「已經完成到哪裡」，由來源自己定義意義（第幾頁、第幾個查詢條件）。
    #:
    #: 沒跑完的執行才有值，跑完就清掉。逐項查詢一趟可能好幾個小時，第 80 個
    #: 條件時網路斷一下就整批重來，那是白做工——這一欄就是為了不要重來。
    resume_state: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<CrawlJob id={self.id} source={self.source!r} status={self.status!r}>"


class EmailMessage(Base):
    """One outbound message, recorded whether or not it was actually sent.

    Written *before* the SMTP handoff and updated after, so a crash mid-batch
    leaves evidence of what was attempted rather than a silent gap. This table
    is also the audit trail that makes the daily send cap enforceable.
    """

    __tablename__ = "email_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"), index=True
    )
    campaign: Mapped[str | None] = mapped_column(String(128), index=True)
    to_address: Mapped[str] = mapped_column(
        EncryptedString(320, lowercase=True), index=True
    )
    subject: Mapped[str] = mapped_column(String(512), default="")
    body: Mapped[str | None] = mapped_column(EncryptedText)
    #: 這封信帶了哪些附件，一行一個檔名。只是稽核紀錄——檔案本身在
    #: ``attachments/``，這裡存的是「當時寄出去的是哪幾個」。用換行分隔而不是
    #: 逗號，因為檔名裡可以有逗號、不會有換行（見 gmail.attachments.safe_name）。
    attachments: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(32), default=EmailStatus.PENDING.value, index=True
    )
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)

    @property
    def attachment_names(self) -> list[str]:
        return [line for line in (self.attachments or "").splitlines() if line.strip()]

    def __repr__(self) -> str:
        return f"<EmailMessage id={self.id} to={self.to_address!r} status={self.status!r}>"


class MXCache(Base):
    """Cached MX lookups so re-verification does not re-hit DNS every run."""

    __tablename__ = "mx_cache"

    domain: Mapped[str] = mapped_column(String(255), primary_key=True)
    has_mx: Mapped[bool] = mapped_column(Boolean, default=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    def __repr__(self) -> str:
        return f"<MXCache {self.domain!r} has_mx={self.has_mx}>"
