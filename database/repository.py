"""Repository layer.

Every read and write goes through a repository, so no other layer builds SQL or
touches the ORM. Each repository is constructed with an open ``Session`` and
does **not** commit -- transaction boundaries belong to
:func:`database.session.session_scope` or to the caller.

Encrypted columns (see :mod:`database.types`) constrain what SQL can do here.
Equality still works, because the encryption is deterministic -- so upserts,
the unique dedupe key and ``name_key + phone`` lookups are unaffected. What does
*not* survive encryption is ordering and substring matching: ciphertext sorts
and ``LIKE``-matches by ciphertext. Those two operations are therefore done in
Python, over rows SQL has already narrowed down as far as it can.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Select, delete, desc, func, or_, select
from sqlalchemy.orm import Session

from core.constants import (
    ActivityType,
    CrawlStatus,
    EmailVerdict,
    LogCategory,
    PipelineStage,
    Priority,
    RecordStatus,
)
from core.errors import DatabaseError
from core.logging_setup import get_logger
from core.scoring import LEAD_SCORE_ORDER, lead_score
from core.schemas import (
    ActivityView,
    CleanCompany,
    CompanyFilter,
    CompanyView,
    ContactView,
    CrawlSummary,
    DashboardStats,
)
from database.models import (
    Activity,
    Attachment,
    Company,
    Contact,
    CrawlJob,
    MXCache,
    Tag,
    now,
)
from database.types import email_equals, is_encrypted_column

log = get_logger(LogCategory.DATABASE)


@dataclass(frozen=True, slots=True)
class CompletionProgress:
    """「補齊公司資料」還剩多少。見 :meth:`CompanyRepository.completion_progress`。"""

    #: 至少還缺一個欄位的家數。
    pending: int = 0
    #: 上面那些裡面，一次都還沒被補齊流程碰過的家數。
    untried: int = 0

    @property
    def tried(self) -> int:
        """試過、但還是缺東西的家數。這一批多半是真的補不到的。"""
        return self.pending - self.untried


# Columns the caller may sort by. Anything else falls back to updated_at,
# which keeps `order_by` safe to wire straight to a GUI dropdown.
SORTABLE_COLUMNS = {
    "id",
    "company_name",
    "email",
    "phone",
    "industry",
    "source",
    "status",
    "pipeline_stage",
    "priority",
    "follow_up_date",
    "created_at",
    "updated_at",
    "capital_amount",
    "registration_status",
    # 這一個不是欄位，是算出來的（見 core.scoring）。SQL 排不了它，
    # search() 會走 Python 端排序那條路。
    LEAD_SCORE_ORDER,
}

# Columns scanned by CompanyFilter.text.
FULL_TEXT_COLUMNS = (
    Company.company_name,
    Company.tax_id,
    Company.email,
    Company.phone,
    Company.website,
    Company.address,
    Company.industry,
    Company.english_name,
    Company.fax,
    Company.products,
    Company.contact_person,
    Company.remark,
    Company.source,
)

# Columns CompanyFilter matches one by one, as a contains-match.
EXACT_ISH_FIELDS = (
    "company_name",
    "email",
    "phone",
    "website",
    "address",
    "industry",
    "english_name",
    "contact_person",
)

#: Names of the encrypted columns among the above. Derived from the model, so
#: encrypting one more column does not need a second edit here.
_ENCRYPTED_TEXT_FIELDS = frozenset(
    column.key for column in FULL_TEXT_COLUMNS if is_encrypted_column(column)
)


def _like(value: str) -> str:
    """Wrap a user term for a case-insensitive contains match."""
    escaped = value.strip().replace("%", r"\%").replace("_", r"\_")
    return f"%{escaped}%"


def _contains(company: Company, field: str, needle: str) -> bool:
    value = getattr(company, field, None)
    return bool(value) and needle in str(value).lower()


def _residual_filter(criteria: CompanyFilter) -> Callable[[Company], bool] | None:
    """The part of ``criteria`` SQL cannot evaluate, as a Python predicate.

    ``None`` means SQL handled everything and the rows can be sliced in the
    database. Free-text search always lands here when any scanned column is
    encrypted: the clause is one big ``OR``, and a row matching *only* on the
    encrypted side would be thrown away by a SQL pre-filter before Python ever
    saw it. So the whole ``OR`` moves to Python rather than half of it.
    """
    checks: list[Callable[[Company], bool]] = []

    if criteria.text and _ENCRYPTED_TEXT_FIELDS:
        needle = criteria.text.strip().lower()
        fields = tuple(column.key for column in FULL_TEXT_COLUMNS)
        checks.append(
            lambda company, _f=fields, _n=needle: any(
                _contains(company, field, _n) for field in _f
            )
        )

    for field in EXACT_ISH_FIELDS:
        value = getattr(criteria, field, None)
        if value and field in _ENCRYPTED_TEXT_FIELDS:
            checks.append(
                lambda company, _f=field, _n=value.strip().lower(): _contains(
                    company, _f, _n
                )
            )

    if not checks:
        return None
    return lambda company: all(check(company) for check in checks)


def _sort_key(field: str) -> Callable[[Company], tuple[int, Any]]:
    """Python-side ordering for a column whose ciphertext SQL cannot sort.

    Empty values are grouped last so a table sorted by e-mail shows the records
    that actually have one first -- the same thing the user wanted when they
    clicked the column.
    """

    def key(company: Company) -> tuple[int, Any]:
        value = getattr(company, field, None)
        if value is None or value == "":
            return (1, "")
        return (0, value.lower() if isinstance(value, str) else value)

    return key


class CompanyRepository:
    """Reads and writes for :class:`~database.models.Company`."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ read

    def get(self, company_id: int) -> Company | None:
        return self.session.get(Company, company_id)

    def get_by_dedupe_key(self, dedupe_key: str) -> Company | None:
        if not dedupe_key:
            return None
        stmt = select(Company).where(Company.dedupe_key == dedupe_key)
        return self.session.execute(stmt).scalars().first()

    def get_by_email(self, email: str) -> Company | None:
        if not email:
            return None
        stmt = select(Company).where(email_equals(Company.email, email))
        return self.session.execute(stmt).scalars().first()

    def get_by_tax_id(self, tax_id: str) -> Company | None:
        if not tax_id:
            return None
        stmt = select(Company).where(Company.tax_id == tax_id.strip())
        return self.session.execute(stmt).scalars().first()

    def all(self) -> list[Company]:
        return list(self.session.execute(select(Company)).scalars())

    def search(self, criteria: CompanyFilter | None = None) -> list[Company]:
        criteria = criteria or CompanyFilter()
        residual = _residual_filter(criteria)

        # 名單品質是算出來的，沒有對應的資料庫欄位，所以只能在 Python 端排。
        by_score = criteria.order_by == LEAD_SCORE_ORDER
        key = lead_score if by_score else None

        sort_field = criteria.order_by if criteria.order_by in SORTABLE_COLUMNS else None
        column = None if by_score else (getattr(Company, sort_field, None) if sort_field else None)
        if column is None and not by_score:
            column, sort_field = Company.updated_at, "updated_at"
        if key is None:
            key = _sort_key(sort_field)
        sort_in_python = by_score or is_encrypted_column(column)

        stmt = self._apply_filters(select(Company), criteria)

        if residual is None and not sort_in_python:
            stmt = stmt.order_by(column.desc() if criteria.descending else column.asc())
            if criteria.offset:
                stmt = stmt.offset(criteria.offset)
            if criteria.limit:
                stmt = stmt.limit(criteria.limit)
            return list(self.session.execute(stmt).scalars().unique())

        # Something SQL cannot do is involved, so paging has to happen after
        # Python has seen every candidate row -- LIMIT in the database would
        # slice the wrong set.
        if not sort_in_python:
            stmt = stmt.order_by(column.desc() if criteria.descending else column.asc())

        rows = list(self.session.execute(stmt).scalars().unique())
        if residual is not None:
            rows = [company for company in rows if residual(company)]
        if sort_in_python:
            rows.sort(key=key, reverse=criteria.descending)

        start = criteria.offset or 0
        end = start + criteria.limit if criteria.limit else None
        return rows[start:end]

    def count(self, criteria: CompanyFilter | None = None) -> int:
        criteria = criteria or CompanyFilter()
        residual = _residual_filter(criteria)
        if residual is None:
            stmt = self._apply_filters(
                select(func.count(func.distinct(Company.id))), criteria
            )
            return int(self.session.execute(stmt).scalar_one())

        stmt = self._apply_filters(select(Company), criteria)
        rows = self.session.execute(stmt).scalars().unique()
        return sum(1 for company in rows if residual(company))

    def _apply_filters(self, stmt: Select, criteria: CompanyFilter) -> Select:
        """Every part of ``criteria`` SQL can evaluate.

        Encrypted columns are skipped here and picked up by
        :func:`_residual_filter`; the two functions must stay complementary, or
        a filter is either applied twice or not at all.
        """
        if criteria.tags:
            stmt = stmt.join(Company.tags).where(Tag.name.in_(criteria.tags))

        if criteria.text and not _ENCRYPTED_TEXT_FIELDS:
            term = _like(criteria.text)
            stmt = stmt.where(or_(*[col.ilike(term) for col in FULL_TEXT_COLUMNS]))

        for field in EXACT_ISH_FIELDS:
            value = getattr(criteria, field, None)
            if value and field not in _ENCRYPTED_TEXT_FIELDS:
                stmt = stmt.where(getattr(Company, field).ilike(_like(value)))

        in_filters = {
            Company.status: criteria.statuses,
            Company.pipeline_stage: criteria.stages,
            Company.priority: criteria.priorities,
            Company.source: criteria.sources,
            Company.email_verdict: criteria.email_verdicts,
        }
        for column, values in in_filters.items():
            if values:
                stmt = stmt.where(column.in_(values))

        if criteria.has_email is True:
            stmt = stmt.where(Company.email.is_not(None), Company.email != "")
        elif criteria.has_email is False:
            stmt = stmt.where(or_(Company.email.is_(None), Company.email == ""))

        if criteria.emailed is True:
            stmt = stmt.where(Company.last_emailed_at.is_not(None))
        elif criteria.emailed is False:
            stmt = stmt.where(Company.last_emailed_at.is_(None))

        if criteria.created_after:
            stmt = stmt.where(Company.created_at >= criteria.created_after)
        if criteria.created_before:
            stmt = stmt.where(Company.created_at <= criteria.created_before)
        if criteria.follow_up_before:
            stmt = stmt.where(
                Company.follow_up_date.is_not(None),
                Company.follow_up_date <= criteria.follow_up_before,
            )
        return stmt

    def count_enrichable(self) -> int:
        """Companies with a website but no e-mail.

        These are the ones worth visiting: the address is usually published on
        the company's own contact page even when the directory did not list it.
        """
        stmt = select(func.count(Company.id)).where(
            Company.website.is_not(None),
            Company.website != "",
            or_(Company.email.is_(None), Company.email == ""),
        )
        return int(self.session.execute(stmt).scalar_one())

    def count_registrable(self, recheck_after_days: int = 180) -> int:
        """有統一編號、而且還沒查過（或查很久了）公司登記資料的家數。

        跟 :meth:`count_enrichable` 一樣，是給介面顯示「按下去會處理幾家」用
        的。沒有統編的一律不算——那一步只能用統編查，見 :mod:`crawler.registry`。
        """
        stale = now() - timedelta(days=recheck_after_days)
        stmt = select(func.count(Company.id)).where(
            Company.tax_id.is_not(None),
            Company.tax_id != "",
            or_(
                Company.registration_checked_at.is_(None),
                Company.registration_checked_at <= stale,
            ),
        )
        return int(self.session.execute(stmt).scalar_one())

    def count_completable(self, fields: Iterable[str]) -> int:
        """至少缺 ``fields`` 其中一個欄位的家數。

        跟 :meth:`count_enrichable`、:meth:`count_registrable` 一樣，是給介面
        顯示「按下去會處理幾家」用的。

        這一支在 Python 端算，不是 SQL——``email``、``phone``、``address``、
        ``contact_person`` 都是加密欄位，SQL 看到的是密文，``!= ''`` 那種
        條件在上面得到的答案是錯的。而且這個數字必須跟
        :func:`crawler.complete.complete_companies` 真正挑出來的那一批一致，
        兩邊用同一個判斷方式才保證得了。
        """
        return self.completion_progress(fields).pending

    def completion_progress(self, fields: Iterable[str]) -> "CompletionProgress":
        """待補的家數，以及其中有幾家**這一輪還沒試過**。

        分批補齊要顯示的是「還有 2699 家要補，其中 2499 家還沒試過」——只有
        總數的話，使用者跑完一批看到數字幾乎沒動（試過但補不到的公司仍然
        算「待補」），會以為程式沒在動。

        跟 :meth:`count_completable` 一樣在 Python 端算，理由見那一支。
        """
        names = tuple(fields)
        pending = 0
        untried = 0
        for company in self.all():
            if not any(not (getattr(company, name, None) or "").strip() for name in names):
                continue
            pending += 1
            if company.completion_checked_at is None:
                untried += 1
        return CompletionProgress(pending=pending, untried=untried)

    def known_identities(self) -> tuple[frozenset[str], frozenset[str]]:
        """``(name_key, tax_id)`` 兩份集合，給爬取時略過已知公司用。

        只選這兩欄，不是整列——這是為了不把幾千筆完整的 ORM 物件（含解密）
        載進記憶體。這兩欄剛好都是明文的，見
        :class:`~crawler.base.KnownCompanies` 說明為什麼只能用它們。
        """
        rows = self.session.execute(select(Company.name_key, Company.tax_id)).all()
        names = {key for key, _ in rows if key}
        tax_ids = {
            digits
            for _, tax_id in rows
            if (digits := "".join(ch for ch in str(tax_id or "") if ch.isdigit()))
        }
        return frozenset(names), frozenset(tax_ids)

    def distinct_values(self, field: str) -> list[str]:
        """Distinct non-empty values of a column, for GUI filter dropdowns."""
        column = getattr(Company, field, None)
        if column is None:
            raise DatabaseError(f"Company has no column {field!r}")
        stmt = select(column).where(column.is_not(None), column != "").distinct()
        return sorted(str(v) for (v,) in self.session.execute(stmt))

    def find_match(self, record: CleanCompany) -> Company | None:
        """Locate the stored row that ``record`` refers to, if any.

        Checked strongest-first. The dedupe key alone is not enough: the same
        company can arrive twice within one crawl with different fields filled
        in (page 1 has a phone, page 2 has a tax id), which produces two
        different keys for one company. Matching on each identity signal
        separately catches that.
        """
        candidate = self.get_by_dedupe_key(record.dedupe_key)
        if candidate is not None:
            return candidate

        if record.tax_id:
            candidate = self.get_by_tax_id(record.tax_id)
            if candidate is not None:
                return candidate

        if record.email:
            candidate = self.get_by_email(record.email)
            if candidate is not None:
                return candidate

        if not record.name_key:
            return None

        # Imported here, not at module scope: verifier.service imports this
        # module, so a top-level import would close the cycle.
        from verifier.normalize import website_host

        if record.phone:
            stmt = select(Company).where(
                Company.name_key == record.name_key, Company.phone == record.phone
            )
            candidate = self.session.execute(stmt).scalars().first()
            if candidate is not None:
                return candidate

        host = website_host(record.website)
        if host:
            stmt = select(Company).where(
                Company.name_key == record.name_key,
                Company.website.is_not(None),
                Company.website.ilike(f"%{host}%"),
            )
            candidate = self.session.execute(stmt).scalars().first()
            if candidate is not None:
                return candidate

        # Last resort: a stored row whose identity was *only* a name.
        #
        # Directories that list names on the index page and contact details
        # behind a per-company detail page produce exactly this. The first
        # crawl stores "n:<name>"; a later one reaches the detail page, finds
        # an address, and therefore arrives carrying "mail:<address>". None of
        # the checks above can connect the two -- the stored row has no e-mail,
        # no phone and no website to match on -- so every enriched company
        # would be inserted a second time.
        #
        # Matching a "n:" key is safe in a way that matching bare name_key is
        # not: that key means the record itself declared it had no stronger
        # signal, which is the same claim two crawls of one directory make.
        stmt = select(Company).where(Company.dedupe_key == f"n:{record.name_key}")
        candidate = self.session.execute(stmt).scalars().first()
        if candidate is not None:
            return candidate

        return None

    # ----------------------------------------------------------------- write

    def upsert(self, record: CleanCompany) -> tuple[Company, bool]:
        """Insert, or merge into the existing record with the same identity.

        Returns ``(company, created)``. On merge only *empty* fields are filled
        in -- a crawl never overwrites data a user has already curated, except
        for provenance and verification fields which always reflect the latest
        run.
        """
        existing = self.find_match(record)

        if existing is None:
            company = Company(
                company_name=record.company_name,
                name_key=record.name_key,
                dedupe_key=record.dedupe_key,
                tax_id=record.tax_id,
                email=record.email,
                phone=record.phone,
                website=record.website,
                address=record.address,
                industry=record.industry,
                english_name=record.english_name,
                fax=record.fax,
                products=record.products,
                contact_person=record.contact_person,
                extra_fields=dict(record.extra_fields),
                source=record.source,
                source_url=record.source_url,
                status=record.status.value,
                remark=record.remark,
                email_verdict=record.email_verdict.value,
                email_checked_at=record.email_checked_at,
                pipeline_stage=PipelineStage.NEW.value,
                priority=Priority.MEDIUM.value,
            )
            self.session.add(company)
            self.session.flush()
            self._sync_contact_person(company, record)
            return company, False

        fillable = (
            "tax_id",
            "email",
            "phone",
            "website",
            "address",
            "industry",
            "english_name",
            "fax",
            "products",
            "contact_person",
        )
        for field in fillable:
            incoming = getattr(record, field)
            if incoming and not getattr(existing, field):
                setattr(existing, field, incoming)

        # 自由欄位逐個 key 補，規則跟固定欄位一致：只填空的，不覆蓋既有值。
        # 整個字典換掉（而不是就地改）是必要的——SQLAlchemy 不會偵測到
        # 字典的就地修改，改了也不會寫回資料庫。
        if record.extra_fields:
            merged = dict(existing.extra_fields or {})
            for key, value in record.extra_fields.items():
                if value and not merged.get(key):
                    merged[key] = value
            if merged != (existing.extra_fields or {}):
                existing.extra_fields = merged

        if record.source_url:
            existing.source_url = record.source_url

        # Adopt the incoming verdict only when it describes the address we
        # actually hold. A later page that happens to omit the email must not
        # downgrade a verified record to "Empty".
        verdict_applies = (
            record.email_verdict is not EmailVerdict.UNKNOWN
            and record.email is not None
            and record.email == existing.email
        )
        if verdict_applies:
            existing.email_verdict = record.email_verdict.value
            existing.email_checked_at = record.email_checked_at
        existing.updated_at = now()
        self._sync_contact_person(existing, record)
        self.session.flush()
        return existing, True

    def _sync_contact_person(self, company: Company, record: CleanCompany) -> None:
        """Promote a captured contact name into a real :class:`Contact` row.

        Without this the name sits only in ``Company.contact_person`` and the
        Contacts page stays empty even after a crawl that found people --
        which reads as "the app never captures contacts" rather than "this
        directory does not publish them".

        Idempotent: re-crawling does not pile up copies of the same person.
        """
        name = (record.contact_person or "").strip()
        if not name:
            return
        if any((c.name or "").strip() == name for c in company.contacts):
            return

        company.contacts.append(
            Contact(
                name=name,
                # The address the directory listed next to the name is the one
                # you would write to in order to reach them.
                email=record.email,
                phone=record.phone,
                is_primary=not company.contacts,
            )
        )
        self.session.flush()

    def create(self, **fields: Any) -> Company:
        company = Company(**fields)
        self.session.add(company)
        self.session.flush()
        return company

    def update(self, company_id: int, **fields: Any) -> Company:
        company = self.get(company_id)
        if company is None:
            raise DatabaseError(f"company {company_id} not found")
        for key, value in fields.items():
            if not hasattr(company, key):
                raise DatabaseError(f"Company has no field {key!r}")
            setattr(company, key, value)
        company.updated_at = now()
        self.session.flush()
        return company

    def delete(self, company_id: int) -> bool:
        company = self.get(company_id)
        if company is None:
            return False
        self.session.delete(company)
        self.session.flush()
        return True

    def crawl_dates(self) -> list[tuple[date, int]]:
        """每一天各收集了幾家公司，最近的排前面。

        使用者是一批一批爬的，所以「哪一天爬的」是他們心裡真正的分組方式
        ——比「第 1 到 200 筆」直覺得多。
        """
        stmt = (
            select(
                func.date(Company.created_at).label("day"),
                func.count(Company.id),
            )
            .group_by("day")
            .order_by(desc("day"))
        )
        results: list[tuple[date, int]] = []
        for raw_day, count in self.session.execute(stmt):
            if raw_day is None:
                continue
            # SQLite 的 date() 回傳字串，其他資料庫可能直接給 date 物件。
            day = raw_day if isinstance(raw_day, date) else date.fromisoformat(str(raw_day))
            results.append((day, int(count)))
        return results

    def delete_by_date(self, day: date) -> int:
        """刪掉某一天收集到的所有公司，回傳刪除筆數。

        逐筆 delete 而不是一次 bulk delete：公司底下的聯絡人、活動記錄、
        附件都靠 ORM 的 cascade 一起清掉，繞過 ORM 會留下孤兒資料。
        這個功能一次最多也就幾百筆，值不得為了速度換掉正確性。
        """
        start = datetime.combine(day, datetime.min.time())
        end = start + timedelta(days=1)
        stmt = select(Company).where(
            Company.created_at >= start, Company.created_at < end
        )
        companies = list(self.session.execute(stmt).scalars())
        for company in companies:
            self.session.delete(company)
        self.session.flush()
        return len(companies)

    def set_stage(self, company_id: int, stage: PipelineStage) -> Company:
        """Move a company through the pipeline and log it to the history."""
        company = self.get(company_id)
        if company is None:
            raise DatabaseError(f"company {company_id} not found")
        previous = company.pipeline_stage
        company.pipeline_stage = stage.value
        company.updated_at = now()
        self.session.add(
            Activity(
                company_id=company.id,
                type=ActivityType.STAGE_CHANGE.value,
                subject=f"{previous} -> {stage.value}",
            )
        )
        self.session.flush()
        return company

    def set_tags(self, company_id: int, tag_names: Iterable[str]) -> Company:
        company = self.get(company_id)
        if company is None:
            raise DatabaseError(f"company {company_id} not found")
        tag_repo = TagRepository(self.session)
        # Deduplicate case-insensitively before resolving: two spellings of one
        # tag resolve to the same Tag row, and assigning it twice violates the
        # association table's composite primary key.
        resolved: dict[str, Tag] = {}
        for name in tag_names:
            clean = name.strip()
            if clean and clean.lower() not in resolved:
                resolved[clean.lower()] = tag_repo.get_or_create(clean)
        company.tags = list(resolved.values())
        company.updated_at = now()
        self.session.flush()
        return company

    # ------------------------------------------------------------ duplicates

    def find_duplicate_groups(self) -> list[list[Company]]:
        """Groups of records that share an email, tax id, or normalized name.

        ``upsert`` prevents most duplicates at write time; this catches the rest
        (manual entry, imports, records whose email arrived after the fact).
        """
        groups: dict[tuple[str, str], list[Company]] = {}
        for company in self.all():
            keys: list[tuple[str, str]] = []
            if company.tax_id:
                keys.append(("tax_id", company.tax_id.strip()))
            if company.email:
                keys.append(("email", company.email.strip().lower()))
            if company.name_key:
                keys.append(("name", company.name_key))
            for key in keys:
                groups.setdefault(key, []).append(company)

        seen_ids: set[frozenset[int]] = set()
        result: list[list[Company]] = []
        for members in groups.values():
            if len(members) < 2:
                continue
            signature = frozenset(c.id for c in members)
            if signature in seen_ids:
                continue
            seen_ids.add(signature)
            result.append(sorted(members, key=lambda c: c.id))
        return result

    def count_duplicates(self) -> int:
        """Number of records that are surplus copies (group size minus one)."""
        return sum(len(group) - 1 for group in self.find_duplicate_groups())

    def merge(self, keep_id: int, drop_ids: Sequence[int]) -> Company:
        """Fold ``drop_ids`` into ``keep_id``, preserving non-empty fields."""
        keeper = self.get(keep_id)
        if keeper is None:
            raise DatabaseError(f"company {keep_id} not found")

        for drop_id in drop_ids:
            if drop_id == keep_id:
                continue
            victim = self.get(drop_id)
            if victim is None:
                continue
            for field in (
                "tax_id", "email", "phone", "website", "address",
                "industry", "english_name", "fax", "products",
                "contact_person", "remark",
            ):
                if not getattr(keeper, field) and getattr(victim, field):
                    setattr(keeper, field, getattr(victim, field))

            if victim.extra_fields:
                merged = dict(keeper.extra_fields or {})
                for key, value in victim.extra_fields.items():
                    if value and not merged.get(key):
                        merged[key] = value
                keeper.extra_fields = merged

            existing_tags = {tag.name for tag in keeper.tags}
            for tag in victim.tags:
                if tag.name not in existing_tags:
                    keeper.tags.append(tag)
                    existing_tags.add(tag.name)

            # Move children by re-parenting the *relationship*, not just the
            # foreign key column. Setting company_id alone leaves each row in
            # the victim's loaded collection, and those relationships cascade
            # delete-orphan -- so deleting the victim would delete the very
            # contacts, activities and attachments just handed to the keeper.
            for collection_name in ("contacts", "activities", "attachments"):
                victim_items = getattr(victim, collection_name)
                keeper_items = getattr(keeper, collection_name)
                for item in list(victim_items):
                    victim_items.remove(item)
                    keeper_items.append(item)

            self.session.flush()
            self.session.delete(victim)

        keeper.updated_at = now()
        self.session.flush()
        return keeper

    # ------------------------------------------------------------------ view

    @staticmethod
    def to_view(company: Company) -> CompanyView:
        return CompanyView(
            id=company.id,
            company_name=company.company_name,
            tax_id=company.tax_id,
            email=company.email,
            phone=company.phone,
            website=company.website,
            address=company.address,
            industry=company.industry,
            english_name=company.english_name,
            fax=company.fax,
            products=company.products,
            contact_person=company.contact_person,
            extra_fields=dict(company.extra_fields or {}),
            source=company.source,
            source_url=company.source_url,
            status=company.status,
            pipeline_stage=company.pipeline_stage,
            priority=company.priority,
            email_verdict=company.email_verdict,
            follow_up_date=company.follow_up_date,
            remark=company.remark,
            tags=company.tag_names(),
            created_at=company.created_at,
            updated_at=company.updated_at,
            capital_amount=company.capital_amount,
            registration_status=company.registration_status,
            registration_checked_at=company.registration_checked_at,
            do_not_contact=company.do_not_contact,
        )

    def search_views(self, criteria: CompanyFilter | None = None) -> list[CompanyView]:
        return [self.to_view(c) for c in self.search(criteria)]


class TagRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_or_create(self, name: str, color: str | None = None) -> Tag:
        clean = name.strip()
        stmt = select(Tag).where(func.lower(Tag.name) == clean.lower())
        tag = self.session.execute(stmt).scalars().first()
        if tag is None:
            tag = Tag(name=clean, color=color)
            self.session.add(tag)
            self.session.flush()
        return tag

    def all(self) -> list[Tag]:
        return list(self.session.execute(select(Tag).order_by(Tag.name)).scalars())

    def names(self) -> list[str]:
        return [tag.name for tag in self.all()]

    def delete(self, name: str) -> bool:
        tag = self.session.execute(
            select(Tag).where(func.lower(Tag.name) == name.strip().lower())
        ).scalars().first()
        if tag is None:
            return False
        self.session.delete(tag)
        self.session.flush()
        return True

    def usage_counts(self) -> dict[str, int]:
        return {tag.name: len(tag.companies) for tag in self.all()}


class ContactRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, contact_id: int) -> Contact | None:
        return self.session.get(Contact, contact_id)

    def add(self, company_id: int, **fields: Any) -> Contact:
        contact = Contact(company_id=company_id, **fields)
        self.session.add(contact)
        self.session.flush()
        return contact

    def update(self, contact_id: int, **fields: Any) -> Contact:
        contact = self.get(contact_id)
        if contact is None:
            raise DatabaseError(f"contact {contact_id} not found")
        for key, value in fields.items():
            setattr(contact, key, value)
        self.session.flush()
        return contact

    def delete(self, contact_id: int) -> bool:
        contact = self.get(contact_id)
        if contact is None:
            return False
        self.session.delete(contact)
        self.session.flush()
        return True

    def for_company(self, company_id: int) -> list[Contact]:
        stmt = select(Contact).where(Contact.company_id == company_id)
        return list(self.session.execute(stmt).scalars())

    def search(self, text: str | None = None, limit: int | None = None) -> list[ContactView]:
        """Contacts, newest-relevant first, optionally narrowed by a term.

        A contact is almost entirely personal data -- name, e-mail, phone and
        mobile are all encrypted -- so neither the term match nor the by-name
        ordering can be expressed in SQL here. Both run in Python over the
        decrypted values. Contacts are hand-entered, so the row count is small.
        """
        stmt = select(Contact, Company.company_name).join(
            Company, Company.id == Contact.company_id
        )
        rows = list(self.session.execute(stmt))

        if text:
            needle = text.strip().lower()

            def matches(contact: Contact, company_name: str) -> bool:
                candidates = (
                    contact.name,
                    contact.email,
                    contact.phone,
                    contact.mobile,
                    contact.title,
                    company_name,
                )
                return any(v and needle in str(v).lower() for v in candidates)

            rows = [row for row in rows if matches(row[0], row[1])]

        rows.sort(key=lambda row: (not row[0].is_primary, (row[0].name or "").lower()))
        if limit:
            rows = rows[:limit]

        views: list[ContactView] = []
        for contact, company_name in rows:
            view = ContactView.model_validate(contact)
            view.company_name = company_name
            views.append(view)
        return views

    def count(self) -> int:
        return int(self.session.execute(select(func.count(Contact.id))).scalar_one())


class ActivityRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self,
        company_id: int,
        type: ActivityType = ActivityType.NOTE,
        subject: str | None = None,
        body: str | None = None,
        occurred_at: datetime | None = None,
    ) -> Activity:
        activity = Activity(
            company_id=company_id,
            type=type.value,
            subject=subject,
            body=body,
            occurred_at=occurred_at or now(),
        )
        self.session.add(activity)
        self.session.flush()
        return activity

    def for_company(self, company_id: int, limit: int = 100) -> list[ActivityView]:
        stmt = (
            select(Activity)
            .where(Activity.company_id == company_id)
            .order_by(Activity.occurred_at.desc())
            .limit(limit)
        )
        return [
            ActivityView.model_validate(a)
            for a in self.session.execute(stmt).scalars()
        ]

    def delete(self, activity_id: int) -> bool:
        activity = self.session.get(Activity, activity_id)
        if activity is None:
            return False
        self.session.delete(activity)
        self.session.flush()
        return True


class AttachmentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self,
        company_id: int,
        filename: str,
        path: str,
        mime_type: str | None = None,
        size_bytes: int = 0,
    ) -> Attachment:
        attachment = Attachment(
            company_id=company_id,
            filename=filename,
            path=path,
            mime_type=mime_type,
            size_bytes=size_bytes,
        )
        self.session.add(attachment)
        self.session.flush()
        return attachment

    def for_company(self, company_id: int) -> list[Attachment]:
        stmt = select(Attachment).where(Attachment.company_id == company_id)
        return list(self.session.execute(stmt).scalars())

    def delete(self, attachment_id: int) -> bool:
        attachment = self.session.get(Attachment, attachment_id)
        if attachment is None:
            return False
        self.session.delete(attachment)
        self.session.flush()
        return True


class CrawlJobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def start(self, source: str) -> CrawlJob:
        job = CrawlJob(source=source, status=CrawlStatus.RUNNING.value, started_at=now())
        self.session.add(job)
        self.session.flush()
        return job

    def finish(self, job: CrawlJob, summary: CrawlSummary) -> CrawlJob:
        job.status = summary.status
        job.pages_crawled = summary.pages_crawled
        job.records_found = summary.records_found
        job.records_new = summary.records_new
        job.records_updated = summary.records_updated
        job.records_duplicate = summary.records_duplicate
        job.records_invalid = summary.records_invalid
        job.error = summary.error
        job.finished_at = summary.finished_at or now()
        self.session.flush()
        return job

    def recent(self, limit: int = 20) -> list[CrawlJob]:
        stmt = select(CrawlJob).order_by(CrawlJob.started_at.desc()).limit(limit)
        return list(self.session.execute(stmt).scalars())

    def previous_for(self, source: str, before_id: int | None = None) -> CrawlJob | None:
        """這個來源上一次的執行紀錄（不含正在跑的這一次）。

        健康度比對與續跑都要它。``before_id`` 是現在這一次的 id——不排除掉的話
        比到的是自己。
        """
        stmt = select(CrawlJob).where(CrawlJob.source == source)
        if before_id is not None:
            stmt = stmt.where(CrawlJob.id != before_id)
        stmt = stmt.order_by(CrawlJob.started_at.desc(), CrawlJob.id.desc()).limit(1)
        return self.session.execute(stmt).scalars().first()

    def last_harvest_for(self, source: str, before_id: int | None = None) -> int | None:
        """這個來源上一次**真的抓到東西**的筆數，沒有就是 None。

        比較的基準要跳過中途取消、失敗、以及本來就抓 0 筆的那些執行——拿一次
        失敗的執行當基準，等於永遠不會再警告。
        """
        stmt = (
            select(CrawlJob.records_found)
            .where(CrawlJob.source == source)
            .where(CrawlJob.records_found > 0)
            .where(CrawlJob.status.in_([CrawlStatus.SUCCESS.value, CrawlStatus.PARTIAL.value]))
        )
        if before_id is not None:
            stmt = stmt.where(CrawlJob.id != before_id)
        stmt = stmt.order_by(CrawlJob.started_at.desc(), CrawlJob.id.desc()).limit(1)
        return self.session.execute(stmt).scalars().first()

    def clear_resume(self, source: str | None = None) -> int:
        """把「做到哪裡」的紀錄清掉，下一次從頭開始。回傳清了幾筆。

        使用者按「取消」而不是「暫停」時用的。兩顆按鈕的差別就只有這一件事，
        而它必須真的做得到——否則「取消」跟「暫停」在行為上完全一樣，那就是
        畫面上多了一顆騙人的按鈕。
        """
        stmt = select(CrawlJob).where(CrawlJob.resume_state.is_not(None))
        if source:
            stmt = stmt.where(CrawlJob.source == source)
        jobs = list(self.session.execute(stmt).scalars())
        for job in jobs:
            job.resume_state = None
        return len(jobs)

    def last(self) -> CrawlJob | None:
        jobs = self.recent(limit=1)
        return jobs[0] if jobs else None

    @staticmethod
    def to_summary(job: CrawlJob) -> CrawlSummary:
        return CrawlSummary(
            source=job.source,
            status=job.status,
            pages_crawled=job.pages_crawled,
            records_found=job.records_found,
            records_new=job.records_new,
            records_updated=job.records_updated,
            records_duplicate=job.records_duplicate,
            records_invalid=job.records_invalid,
            started_at=job.started_at,
            finished_at=job.finished_at,
            error=job.error,
        )


class MXCacheRepository:
    """Persistent MX-lookup cache, so verification does not hammer DNS."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def lookup(self, domain: str, max_age_hours: int) -> bool | None:
        """Cached answer, or ``None`` when absent or stale."""
        entry = self.session.get(MXCache, domain.lower())
        if entry is None:
            return None
        if max_age_hours and entry.checked_at < now() - timedelta(hours=max_age_hours):
            return None
        return entry.has_mx

    def store(self, domain: str, has_mx: bool) -> None:
        key = domain.lower()
        entry = self.session.get(MXCache, key)
        if entry is None:
            self.session.add(MXCache(domain=key, has_mx=has_mx, checked_at=now()))
        else:
            entry.has_mx = has_mx
            entry.checked_at = now()
        self.session.flush()

    def clear(self) -> int:
        result = self.session.execute(delete(MXCache))
        self.session.flush()
        return int(result.rowcount or 0)


class StatsRepository:
    """Aggregations for the Dashboard page."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def dashboard(self) -> DashboardStats:
        session = self.session
        today = date.today()
        midnight = datetime.combine(today, datetime.min.time())
        week_ago = midnight - timedelta(days=7)

        total = int(session.execute(select(func.count(Company.id))).scalar_one())
        with_email = int(
            session.execute(
                select(func.count(Company.id)).where(
                    Company.email.is_not(None), Company.email != ""
                )
            ).scalar_one()
        )
        verified = int(
            session.execute(
                select(func.count(Company.id)).where(
                    Company.email_verdict == EmailVerdict.VALID.value
                )
            ).scalar_one()
        )
        new_today = int(
            session.execute(
                select(func.count(Company.id)).where(Company.created_at >= midnight)
            ).scalar_one()
        )
        new_week = int(
            session.execute(
                select(func.count(Company.id)).where(Company.created_at >= week_ago)
            ).scalar_one()
        )
        follow_ups = int(
            session.execute(
                select(func.count(Company.id)).where(
                    Company.follow_up_date.is_not(None),
                    Company.follow_up_date <= today,
                    Company.status == RecordStatus.ACTIVE.value,
                )
            ).scalar_one()
        )

        by_stage = {
            stage: count
            for stage, count in session.execute(
                select(Company.pipeline_stage, func.count(Company.id)).group_by(
                    Company.pipeline_stage
                )
            )
        }
        by_source = {
            (source or "unknown"): count
            for source, count in session.execute(
                select(Company.source, func.count(Company.id))
                .group_by(Company.source)
                .order_by(func.count(Company.id).desc())
                .limit(10)
            )
        }

        last_job = CrawlJobRepository(session).last()
        return DashboardStats(
            total_companies=total,
            total_emails=with_email,
            verified_emails=verified,
            total_contacts=ContactRepository(session).count(),
            new_today=new_today,
            new_this_week=new_week,
            duplicates=CompanyRepository(session).count_duplicates(),
            follow_ups_due=follow_ups,
            by_stage=by_stage,
            by_source=by_source,
            last_crawl=CrawlJobRepository.to_summary(last_job) if last_job else None,
        )
