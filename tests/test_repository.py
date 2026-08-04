"""Tests for database/repository.py."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from core.constants import ActivityType, CrawlStatus, EmailVerdict, PipelineStage, RecordStatus
from core.errors import DatabaseError
from core.schemas import CleanCompany, CompanyFilter, CrawlSummary
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
from sqlalchemy.exc import SQLAlchemyError


def make_clean(
    name: str,
    name_key: str,
    dedupe_key: str,
    **kwargs,
) -> CleanCompany:
    return CleanCompany(company_name=name, name_key=name_key, dedupe_key=dedupe_key, **kwargs)


# --------------------------------------------------------------------- upsert


def test_upsert_inserts_new_company(db_session):
    repo = CompanyRepository(db_session)
    record = make_clean("Foo Co", "foo", "tax:22099131", tax_id="22099131", email="a@b.com")
    company, created = repo.upsert(record)
    assert created is False  # created=False means "not merged", i.e. a fresh insert
    assert company.id is not None
    assert company.company_name == "Foo Co"
    assert company.pipeline_stage == PipelineStage.NEW.value


def test_upsert_merges_matching_dedupe_key_filling_only_gaps(db_session):
    repo = CompanyRepository(db_session)
    first = make_clean("Foo Co", "foo", "tax:22099131", tax_id="22099131", phone="02-1111")
    repo.upsert(first)

    second = make_clean(
        "Foo Co",
        "foo",
        "tax:22099131",
        tax_id="22099131",
        phone="02-2222",  # existing already has a phone -> must not be overwritten
        address="台北市信義區",  # existing has none -> should be filled in
    )
    company, merged = repo.upsert(second)
    assert merged is True
    assert company.phone == "02-1111"
    assert company.address == "台北市信義區"


def test_upsert_does_not_downgrade_email_verdict_when_later_record_has_no_email(db_session):
    """Regression test for a real bug: a later page missing the email must not
    reset a previously verified address back to "Empty"."""
    repo = CompanyRepository(db_session)
    first = make_clean(
        "Foo Co",
        "foo",
        "tax:22099131",
        tax_id="22099131",
        email="a@b.com",
        email_verdict=EmailVerdict.VALID,
        email_checked_at=datetime(2026, 1, 1, 12, 0, 0),
    )
    company, _ = repo.upsert(first)
    assert company.email_verdict == EmailVerdict.VALID.value

    second = make_clean(
        "Foo Co",
        "foo",
        "tax:22099131",
        tax_id="22099131",
        email=None,
        email_verdict=EmailVerdict.EMPTY,
    )
    same_company, merged = repo.upsert(second)

    assert merged is True
    assert same_company.id == company.id
    assert same_company.email == "a@b.com"
    assert same_company.email_verdict == EmailVerdict.VALID.value


def test_upsert_adopts_new_verdict_when_it_describes_the_held_address(db_session):
    repo = CompanyRepository(db_session)
    first = make_clean(
        "Foo Co", "foo", "mail:a@b.com", email="a@b.com", email_verdict=EmailVerdict.UNKNOWN
    )
    repo.upsert(first)

    second = make_clean(
        "Foo Co", "foo", "mail:a@b.com", email="a@b.com", email_verdict=EmailVerdict.VALID
    )
    company, merged = repo.upsert(second)
    assert merged is True
    assert company.email_verdict == EmailVerdict.VALID.value


# ----------------------------------------------------------------- find_match


def test_find_match_by_tax_id_despite_different_dedupe_key(db_session):
    repo = CompanyRepository(db_session)
    existing = repo.create(
        company_name="Foo", name_key="foo", dedupe_key="np:foo|02-1234", tax_id="22099131"
    )
    record = make_clean("Foo", "foo", "tax:22099131", tax_id="22099131")
    match = repo.find_match(record)
    assert match is not None
    assert match.id == existing.id


def test_find_match_by_email_despite_different_dedupe_key(db_session):
    repo = CompanyRepository(db_session)
    existing = repo.create(
        company_name="Bar", name_key="bar", dedupe_key="np:bar|02-5555", email="X@Y.com"
    )
    record = make_clean("Bar", "bar", "mail:x@y.com", email="x@y.com")
    match = repo.find_match(record)
    assert match is not None
    assert match.id == existing.id


def test_find_match_by_name_key_and_phone_despite_different_dedupe_key(db_session):
    repo = CompanyRepository(db_session)
    existing = repo.create(
        company_name="Baz", name_key="baz", dedupe_key="raw:something", phone="02-9999"
    )
    record = make_clean("Baz", "baz", "np:baz|02-9999", phone="02-9999")
    match = repo.find_match(record)
    assert match is not None
    assert match.id == existing.id


def test_find_match_by_name_key_and_website_host_despite_different_dedupe_key(db_session):
    repo = CompanyRepository(db_session)
    existing = repo.create(
        company_name="Qux",
        name_key="qux",
        dedupe_key="raw:qux-page",
        website="https://Example.com/page",
    )
    record = make_clean(
        "Qux", "qux", "nw:qux|example.com", website="https://www.example.com"
    )
    match = repo.find_match(record)
    assert match is not None
    assert match.id == existing.id


def test_find_match_returns_none_when_nothing_matches(db_session):
    repo = CompanyRepository(db_session)
    record = make_clean("Nobody", "nobody", "n:nobody")
    assert repo.find_match(record) is None


def test_find_match_returns_none_when_name_key_is_blank(db_session):
    repo = CompanyRepository(db_session)
    record = make_clean("###", "", "raw:###")
    assert repo.find_match(record) is None


# ------------------------------------------------------------- create/update


def test_create_and_update_and_delete(db_session):
    repo = CompanyRepository(db_session)
    company = repo.create(company_name="Foo", name_key="foo", dedupe_key="n:foo")

    updated = repo.update(company.id, company_name="Foo Renamed", phone="02-1111")
    assert updated.company_name == "Foo Renamed"
    assert updated.phone == "02-1111"

    assert repo.delete(company.id) is True
    assert repo.delete(company.id) is False
    assert repo.get(company.id) is None


def test_update_missing_company_raises(db_session):
    repo = CompanyRepository(db_session)
    with pytest.raises(DatabaseError):
        repo.update(999, company_name="x")


def test_update_unknown_field_raises(db_session):
    repo = CompanyRepository(db_session)
    company = repo.create(company_name="Foo", name_key="foo", dedupe_key="n:foo2")
    with pytest.raises(DatabaseError):
        repo.update(company.id, not_a_real_field="x")


# --------------------------------------------------------------------- search


@pytest.fixture
def populated_repo(db_session):
    repo = CompanyRepository(db_session)
    a = repo.create(
        company_name="Alpha Metals",
        name_key="alpha",
        dedupe_key="tax:11111111",
        email="a@alpha.tw",
        industry="Metal",
        pipeline_stage=PipelineStage.NEW.value,
        status=RecordStatus.ACTIVE.value,
        source="crawl-a",
    )
    b = repo.create(
        company_name="Beta Textiles",
        name_key="beta",
        dedupe_key="tax:22222222",
        email=None,
        industry="Textile",
        pipeline_stage=PipelineStage.QUALIFIED.value,
        status=RecordStatus.ACTIVE.value,
        source="crawl-b",
    )
    c = repo.create(
        company_name="Gamma Chemicals",
        name_key="gamma",
        dedupe_key="tax:33333333",
        email="c@gamma.tw",
        industry="Chemical",
        pipeline_stage=PipelineStage.QUALIFIED.value,
        status=RecordStatus.ARCHIVED.value,
        source="crawl-a",
    )
    db_session.commit()
    TagRepository(db_session).get_or_create("VIP")
    repo.set_tags(a.id, ["VIP"])
    return repo, (a, b, c)


def test_search_text_matches_across_columns(populated_repo):
    repo, (a, b, c) = populated_repo
    results = repo.search(CompanyFilter(text="alpha"))
    assert [r.id for r in results] == [a.id]


def test_search_by_tags(populated_repo):
    repo, (a, b, c) = populated_repo
    results = repo.search(CompanyFilter(tags=["VIP"]))
    assert [r.id for r in results] == [a.id]


def test_search_by_stage(populated_repo):
    repo, (a, b, c) = populated_repo
    results = repo.search(CompanyFilter(stages=[PipelineStage.QUALIFIED.value]))
    assert {r.id for r in results} == {b.id, c.id}


def test_search_has_email_true_and_false(populated_repo):
    repo, (a, b, c) = populated_repo
    with_email = repo.search(CompanyFilter(has_email=True))
    assert {r.id for r in with_email} == {a.id, c.id}
    without_email = repo.search(CompanyFilter(has_email=False))
    assert {r.id for r in without_email} == {b.id}


def test_search_by_statuses(populated_repo):
    repo, (a, b, c) = populated_repo
    results = repo.search(CompanyFilter(statuses=[RecordStatus.ARCHIVED.value]))
    assert [r.id for r in results] == [c.id]


def test_search_by_sources(populated_repo):
    repo, (a, b, c) = populated_repo
    results = repo.search(CompanyFilter(sources=["crawl-a"]))
    assert {r.id for r in results} == {a.id, c.id}


def test_search_created_after_excludes_older_rows(populated_repo):
    repo, (a, b, c) = populated_repo
    future = datetime.now() + timedelta(days=1)
    results = repo.search(CompanyFilter(created_after=future))
    assert results == []


def test_search_ordering_and_descending(populated_repo):
    repo, (a, b, c) = populated_repo
    ascending = repo.search(
        CompanyFilter(order_by="company_name", descending=False)
    )
    assert [r.company_name for r in ascending] == [
        "Alpha Metals",
        "Beta Textiles",
        "Gamma Chemicals",
    ]
    descending = repo.search(CompanyFilter(order_by="company_name", descending=True))
    assert [r.company_name for r in descending] == list(reversed([r.company_name for r in ascending]))


def test_search_unsortable_column_falls_back_to_updated_at(populated_repo):
    repo, _ = populated_repo
    # "website" is not in SORTABLE_COLUMNS; this must not raise.
    results = repo.search(CompanyFilter(order_by="website"))
    assert len(results) == 3


def test_search_unknown_column_name_falls_back_to_updated_at(populated_repo):
    repo, _ = populated_repo
    # A name that is not even a real Company column must also fall back
    # rather than raise (``getattr(Company, ..., None)`` guards it).
    results = repo.search(CompanyFilter(order_by="not_a_real_column"))
    assert len(results) == 3


def test_search_limit_and_offset(populated_repo):
    repo, (a, b, c) = populated_repo
    page1 = repo.search(CompanyFilter(order_by="company_name", descending=False, limit=1, offset=0))
    page2 = repo.search(CompanyFilter(order_by="company_name", descending=False, limit=1, offset=1))
    assert len(page1) == 1
    assert len(page2) == 1
    assert page1[0].id != page2[0].id


def test_count(populated_repo):
    repo, _ = populated_repo
    assert repo.count() == 3
    assert repo.count(CompanyFilter(text="alpha")) == 1


def test_distinct_values(populated_repo):
    repo, _ = populated_repo
    assert repo.distinct_values("industry") == ["Chemical", "Metal", "Textile"]


def test_distinct_values_unknown_column_raises(populated_repo):
    repo, _ = populated_repo
    with pytest.raises(DatabaseError):
        repo.distinct_values("not_a_real_column")


# ------------------------------------------------------------------ set_stage


def test_set_stage_updates_and_logs_activity(db_session):
    repo = CompanyRepository(db_session)
    company = repo.create(company_name="Foo", name_key="foo", dedupe_key="n:foo")
    updated = repo.set_stage(company.id, PipelineStage.QUALIFIED)
    assert updated.pipeline_stage == PipelineStage.QUALIFIED.value

    activities = ActivityRepository(db_session).for_company(company.id)
    assert len(activities) == 1
    assert activities[0].type == ActivityType.STAGE_CHANGE.value
    assert activities[0].subject == "New -> Qualified"


def test_set_stage_missing_company_raises(db_session):
    repo = CompanyRepository(db_session)
    with pytest.raises(DatabaseError):
        repo.set_stage(999, PipelineStage.WON)


# ------------------------------------------------------------------- set_tags


def test_set_tags_assigns_and_skips_blanks(db_session):
    repo = CompanyRepository(db_session)
    company = repo.create(company_name="Foo", name_key="foo", dedupe_key="n:foo")
    updated = repo.set_tags(company.id, ["A", "B", "  "])
    assert sorted(t.name for t in updated.tags) == ["A", "B"]


def test_set_tags_deduplicates_repeated_names(db_session):
    """A repeated tag name must collapse, not blow up.

    The same name twice resolves to one :class:`Tag` row, and assigning it
    twice would violate the ``company_tags`` composite primary key. A GUI that
    lets a user type "A, A" should not produce an IntegrityError.
    """
    repo = CompanyRepository(db_session)
    company = repo.create(company_name="Foo", name_key="foo", dedupe_key="n:foo2")

    updated = repo.set_tags(company.id, ["A", "A", "b", "B", "  "])

    assert sorted(t.name for t in updated.tags) == ["A", "b"]


def test_set_tags_missing_company_raises(db_session):
    repo = CompanyRepository(db_session)
    with pytest.raises(DatabaseError):
        repo.set_tags(999, ["A"])


# ------------------------------------------------------------------ duplicates


def test_find_duplicate_groups_and_count(db_session):
    repo = CompanyRepository(db_session)
    x1 = repo.create(company_name="X1", name_key="samekey", dedupe_key="n:samekey1", tax_id="11111111")
    x2 = repo.create(company_name="X2", name_key="samekey", dedupe_key="n:samekey2", tax_id="11111111")
    y1 = repo.create(company_name="Y1", name_key="ykey", dedupe_key="mail:y@z.com", email="y@z.com")
    y2 = repo.create(company_name="Y2", name_key="ykey2", dedupe_key="mail:y@z.com2", email="Y@Z.com")
    solo = repo.create(company_name="Solo", name_key="solo", dedupe_key="n:solo")

    groups = repo.find_duplicate_groups()
    group_ids = {frozenset(c.id for c in g) for g in groups}
    assert frozenset({x1.id, x2.id}) in group_ids
    assert frozenset({y1.id, y2.id}) in group_ids
    assert not any(solo.id in ids for ids in group_ids)

    assert repo.count_duplicates() == 2  # two groups of size 2 -> one surplus each


# ------------------------------------------------------------------------ merge


def test_merge_moves_fields_and_tags_and_deletes_victim(db_session):
    repo = CompanyRepository(db_session)
    tag_repo = TagRepository(db_session)

    keeper = repo.create(company_name="Keeper", name_key="keeper", dedupe_key="n:keeper")
    victim = repo.create(
        company_name="Victim",
        name_key="victim",
        dedupe_key="n:victim",
        email="victim@example.com",
        phone="02-1234",
    )
    repo.set_tags(victim.id, ["Imported"])

    merged = repo.merge(keeper.id, [victim.id])

    assert merged.id == keeper.id
    assert merged.email == "victim@example.com"
    assert merged.phone == "02-1234"
    assert {t.name for t in merged.tags} == {"Imported"}
    assert repo.get(victim.id) is None


def test_merge_carries_child_rows_over_to_the_keeper(db_session):
    """Merging must move the victim's children, not destroy them.

    The children's relationships cascade ``all, delete-orphan``. Re-pointing
    only ``company_id`` leaves each row in the victim's loaded collection, so
    deleting the victim deletes the rows that were just handed to the keeper --
    silent data loss on every merge. They have to be re-parented through the
    relationship itself.
    """
    repo = CompanyRepository(db_session)
    contact_repo = ContactRepository(db_session)
    activity_repo = ActivityRepository(db_session)
    attachment_repo = AttachmentRepository(db_session)

    keeper = repo.create(company_name="Keeper2", name_key="keeper2", dedupe_key="n:keeper2b")
    victim = repo.create(company_name="Victim2", name_key="victim2", dedupe_key="n:victim2b")
    contact_repo.add(victim.id, name="Contact Person")
    activity_repo.add(victim.id, type=ActivityType.NOTE, subject="hello")
    attachment_repo.add(victim.id, filename="f.txt", path="/tmp/f.txt")

    repo.merge(keeper.id, [victim.id])
    db_session.commit()

    contacts = contact_repo.for_company(keeper.id)
    activities = activity_repo.for_company(keeper.id)
    attachments = attachment_repo.for_company(keeper.id)

    assert [c.name for c in contacts] == ["Contact Person"]
    assert [a.subject for a in activities] == ["hello"]
    assert [a.filename for a in attachments] == ["f.txt"]
    assert repo.get(victim.id) is None


def test_merge_missing_keeper_raises(db_session):
    repo = CompanyRepository(db_session)
    with pytest.raises(DatabaseError):
        repo.merge(999, [1])


def test_merge_skips_missing_and_self_ids(db_session):
    repo = CompanyRepository(db_session)
    keeper = repo.create(company_name="Keeper", name_key="keeper", dedupe_key="n:keeper2")
    result = repo.merge(keeper.id, [keeper.id, 999])
    assert result.id == keeper.id


# ---------------------------------------------------------------------- tags


def test_tag_repository_get_or_create_is_case_insensitive(db_session):
    tag_repo = TagRepository(db_session)
    one = tag_repo.get_or_create("VIP")
    two = tag_repo.get_or_create("vip")
    assert one.id == two.id


def test_tag_repository_delete_and_usage_counts(db_session):
    repo = CompanyRepository(db_session)
    tag_repo = TagRepository(db_session)
    company = repo.create(company_name="Foo", name_key="foo", dedupe_key="n:foo")
    repo.set_tags(company.id, ["VIP"])

    assert tag_repo.usage_counts() == {"VIP": 1}
    assert tag_repo.names() == ["VIP"]
    assert tag_repo.delete("vip") is True
    assert tag_repo.delete("vip") is False
    assert tag_repo.names() == []


# ------------------------------------------------------------------- contacts


def test_contact_repository_crud_and_search(db_session):
    repo = CompanyRepository(db_session)
    contact_repo = ContactRepository(db_session)
    company = repo.create(company_name="Foo", name_key="foo", dedupe_key="n:foo")

    contact = contact_repo.add(company.id, name="Jane Doe", email="jane@example.com", is_primary=True)
    assert contact.id is not None
    assert contact_repo.count() == 1

    updated = contact_repo.update(contact.id, title="Manager")
    assert updated.title == "Manager"

    results = contact_repo.search(text="Jane")
    assert len(results) == 1
    assert results[0].company_name == "Foo"

    assert contact_repo.for_company(company.id)[0].id == contact.id

    assert contact_repo.delete(contact.id) is True
    assert contact_repo.delete(contact.id) is False


def test_contact_repository_update_missing_raises(db_session):
    contact_repo = ContactRepository(db_session)
    with pytest.raises(DatabaseError):
        contact_repo.update(999, title="x")


def test_contact_repository_search_respects_limit(db_session):
    repo = CompanyRepository(db_session)
    contact_repo = ContactRepository(db_session)
    company = repo.create(company_name="Foo", name_key="foo", dedupe_key="n:foo3")
    contact_repo.add(company.id, name="Alice")
    contact_repo.add(company.id, name="Bob")

    results = contact_repo.search(limit=1)
    assert len(results) == 1


# ----------------------------------------------------------------- attachments


def test_attachment_repository_crud(db_session):
    repo = CompanyRepository(db_session)
    attachment_repo = AttachmentRepository(db_session)
    company = repo.create(company_name="Foo", name_key="foo", dedupe_key="n:foo4")

    attachment = attachment_repo.add(
        company.id, filename="brochure.pdf", path="/data/brochure.pdf", mime_type="application/pdf"
    )
    assert attachment.id is not None
    assert attachment_repo.for_company(company.id)[0].filename == "brochure.pdf"
    assert attachment_repo.delete(attachment.id) is True
    assert attachment_repo.delete(attachment.id) is False


# ------------------------------------------------------------------ activities


def test_activity_repository_add_and_delete(db_session):
    repo = CompanyRepository(db_session)
    activity_repo = ActivityRepository(db_session)
    company = repo.create(company_name="Foo", name_key="foo", dedupe_key="n:foo")

    activity = activity_repo.add(company.id, type=ActivityType.CALL, subject="Called")
    assert activity.id is not None
    assert len(activity_repo.for_company(company.id)) == 1
    assert activity_repo.delete(activity.id) is True
    assert activity_repo.delete(activity.id) is False


# ----------------------------------------------------------------- crawl jobs


def test_crawl_job_repository_start_finish_recent_last(db_session):
    job_repo = CrawlJobRepository(db_session)
    job = job_repo.start("sample")
    assert job.status == CrawlStatus.RUNNING.value

    summary = CrawlSummary(
        source="sample",
        status=CrawlStatus.SUCCESS.value,
        pages_crawled=2,
        records_found=5,
        records_new=4,
        records_updated=1,
        records_duplicate=1,
        records_invalid=0,
        finished_at=datetime.now(),
    )
    finished = job_repo.finish(job, summary)
    assert finished.status == CrawlStatus.SUCCESS.value
    assert finished.records_new == 4

    assert job_repo.last().id == job.id
    assert len(job_repo.recent(limit=10)) == 1

    view = job_repo.to_summary(finished)
    assert view.source == "sample"
    assert view.records_new == 4


# --------------------------------------------------------------------- mx cache


def test_mx_cache_repository_lookup_store_and_expiry(db_session):
    from database.models import MXCache

    cache = MXCacheRepository(db_session)
    assert cache.lookup("example.com", max_age_hours=24) is None

    cache.store("example.com", True)
    assert cache.lookup("EXAMPLE.com", max_age_hours=24) is True

    entry = db_session.get(MXCache, "example.com")
    entry.checked_at = datetime.now() - timedelta(hours=100)
    db_session.flush()
    assert cache.lookup("example.com", max_age_hours=24) is None

    assert cache.clear() == 1
    assert cache.lookup("example.com", max_age_hours=24) is None


def test_mx_cache_repository_store_updates_existing_entry(db_session):
    cache = MXCacheRepository(db_session)
    cache.store("example.com", True)
    cache.store("example.com", False)  # exercises the "entry already exists" branch
    assert cache.lookup("example.com", max_age_hours=24) is False


# ---------------------------------------------------------------------- stats


def test_stats_repository_dashboard(db_session):
    repo = CompanyRepository(db_session)
    job_repo = CrawlJobRepository(db_session)

    a = repo.create(
        company_name="Foo",
        name_key="foo",
        dedupe_key="tax:11111111",
        email="foo@example.com",
        email_verdict=EmailVerdict.VALID.value,
        source="sample",
    )
    repo.create(
        company_name="Bar",
        name_key="bar",
        dedupe_key="tax:22222222",
        source="sample",
        follow_up_date=date.today() - timedelta(days=1),
        status=RecordStatus.ACTIVE.value,
    )
    ContactRepository(db_session).add(a.id, name="Contact A")

    job = job_repo.start("sample")
    job_repo.finish(
        job,
        CrawlSummary(source="sample", status=CrawlStatus.SUCCESS.value, finished_at=datetime.now()),
    )

    stats = StatsRepository(db_session).dashboard()
    assert stats.total_companies == 2
    assert stats.total_emails == 1
    assert stats.verified_emails == 1
    assert stats.total_contacts == 1
    assert stats.new_today == 2
    assert stats.new_this_week == 2
    assert stats.follow_ups_due == 1
    assert stats.by_source.get("sample") == 2
    assert stats.last_crawl is not None
    assert stats.last_crawl.source == "sample"


# ------------------------------------------------------------ core/constants


def test_strenum_str_returns_the_value():
    assert str(EmailVerdict.VALID) == "Valid"
    assert f"{PipelineStage.NEW}" == "New"


def test_strenum_values_lists_every_member():
    assert RecordStatus.values() == ["Active", "Duplicate", "Invalid", "Archived"]


def test_strenum_coerce_matches_case_insensitively():
    assert EmailVerdict.coerce("valid") is EmailVerdict.VALID
    assert EmailVerdict.coerce(" DISPOSABLE ") is EmailVerdict.DISPOSABLE
    assert EmailVerdict.coerce(EmailVerdict.NO_MX) is EmailVerdict.NO_MX


def test_strenum_coerce_falls_back_to_default():
    assert EmailVerdict.coerce("not-a-real-verdict", EmailVerdict.UNKNOWN) is EmailVerdict.UNKNOWN


def test_strenum_coerce_raises_without_a_default():
    with pytest.raises(ValueError):
        EmailVerdict.coerce("not-a-real-verdict")


# ---------------------------------------------------------- database/session


def test_create_db_engine_creates_sqlite_parent_directory(tmp_config, tmp_path):
    import database.session as session_module

    db_path = tmp_path / "nested" / "dir" / "crm.db"
    engine = session_module.create_db_engine(tmp_config, url=f"sqlite:///{db_path.as_posix()}")
    try:
        assert db_path.parent.is_dir()
    finally:
        engine.dispose()


def test_create_db_engine_builds_for_an_in_memory_sqlite_url(tmp_config):
    import database.session as session_module

    engine = session_module.create_db_engine(tmp_config, url="sqlite:///:memory:")
    try:
        assert str(engine.url) == "sqlite:///:memory:"
    finally:
        engine.dispose()


def test_get_engine_and_session_factory_are_cached_and_reset(patch_config):
    import database.session as session_module

    session_module.reset_engine()
    try:
        first_engine = session_module.get_engine()
        assert session_module.get_engine() is first_engine

        first_factory = session_module.get_session_factory()
        assert session_module.get_session_factory() is first_factory

        session_module.reset_engine()
        assert session_module.get_engine() is not first_engine
    finally:
        session_module.reset_engine()


def test_init_db_creates_tables_and_runs_migration(patch_config):
    from sqlalchemy import inspect

    import database.session as session_module
    from database.models import Base

    session_module.reset_engine()
    try:
        session_module.init_db()
        inspector = inspect(session_module.get_engine())
        assert set(Base.metadata.tables) <= set(inspector.get_table_names())
    finally:
        session_module.reset_engine()


def test_session_scope_commits_on_success(patch_config):
    import database.session as session_module

    session_module.reset_engine()
    try:
        session_module.init_db()
        with session_module.session_scope() as session:
            CompanyRepository(session).create(
                company_name="Foo", name_key="foo", dedupe_key="n:foo-session"
            )
        with session_module.session_scope() as session:
            assert CompanyRepository(session).count() == 1
    finally:
        session_module.reset_engine()


def test_session_scope_rolls_back_and_wraps_sqlalchemy_errors(patch_config):
    import database.session as session_module

    session_module.reset_engine()
    try:
        session_module.init_db()
        with pytest.raises(DatabaseError):
            with session_module.session_scope() as session:
                raise SQLAlchemyError("boom")
    finally:
        session_module.reset_engine()


def test_session_scope_rolls_back_and_reraises_other_exceptions(patch_config):
    import database.session as session_module

    session_module.reset_engine()
    try:
        session_module.init_db()
        with pytest.raises(ValueError):
            with session_module.session_scope() as session:
                raise ValueError("not a database problem")
    finally:
        session_module.reset_engine()


# --------------------------------------------------- 補抓信箱的候選公司


def test_count_enrichable_wants_a_website_and_no_email(db_session):
    """只有「有網址、缺信箱」的公司值得去拜訪它的官網。"""
    repo = CompanyRepository(db_session)
    repo.create(
        company_name="有網址沒信箱", dedupe_key="n:1", website="https://a.example"
    )
    repo.create(
        company_name="兩者都有", dedupe_key="n:2",
        website="https://b.example", email="b@example.com",
    )
    repo.create(company_name="兩者都沒有", dedupe_key="n:3")
    repo.create(company_name="只有信箱", dedupe_key="n:4", email="d@example.com")
    db_session.commit()

    assert repo.count_enrichable() == 1


def test_count_enrichable_treats_empty_strings_as_missing(db_session):
    repo = CompanyRepository(db_session)
    repo.create(company_name="空字串", dedupe_key="n:1", website="", email="")
    repo.create(company_name="空信箱", dedupe_key="n:2", website="https://a.example", email="")
    db_session.commit()

    assert repo.count_enrichable() == 1


# ------------------------------------- 名錄「清單頁只有名字」的重複問題


def _clean(name: str, **overrides) -> CleanCompany:
    fields = {
        "company_name": name,
        "name_key": name,
        "dedupe_key": f"n:{name}",
        "source": "unit-test",
        "email_verdict": EmailVerdict.UNKNOWN,
        "status": RecordStatus.ACTIVE,
    }
    fields.update(overrides)
    return CleanCompany(**fields)


def test_a_name_only_record_is_matched_once_it_gains_an_email(db_session):
    """名錄清單頁只有公司名，詳細頁才有信箱——兩次爬取必須合併成一筆。

    第一次存成 ``n:<名稱>``，第二次帶著信箱所以是 ``mail:<信箱>``。少了這個
    比對，重爬一次就會把每一家補到資料的公司再新增一遍。
    """
    repo = CompanyRepository(db_session)
    repo.upsert(_clean("台灣電力修護處"))
    db_session.commit()

    _, merged = repo.upsert(
        _clean(
            "台灣電力修護處",
            dedupe_key="mail:a@taipower.com.tw",
            email="a@taipower.com.tw",
            phone="02-27853199",
        )
    )
    db_session.commit()

    assert merged is True
    assert len(repo.all()) == 1
    stored = repo.all()[0]
    assert stored.email == "a@taipower.com.tw"
    assert stored.phone == "02-27853199"


def test_a_name_only_row_does_not_swallow_a_different_company(db_session):
    """只在名稱正規化後真的相同時才合併。"""
    repo = CompanyRepository(db_session)
    repo.upsert(_clean("甲公司"))
    db_session.commit()

    _, merged = repo.upsert(
        _clean("乙公司", dedupe_key="mail:b@example.com", email="b@example.com")
    )
    db_session.commit()

    assert merged is False
    assert len(repo.all()) == 2


def test_a_row_with_a_stronger_key_is_not_matched_by_name_alone(db_session):
    """已經有統編的紀錄，不該被同名但不同統編的紀錄比中。

    ``n:`` 的比對只針對「當初就只有名字」的那些列。
    """
    repo = CompanyRepository(db_session)
    repo.upsert(_clean("同名公司", dedupe_key="tax:22099131", tax_id="22099131"))
    db_session.commit()

    _, merged = repo.upsert(
        _clean("同名公司", dedupe_key="tax:04595257", tax_id="04595257")
    )
    db_session.commit()

    assert merged is False
    assert len(repo.all()) == 2


# ------------------------------------------------ 爬到的聯絡人要進聯絡人頁


def test_a_captured_contact_person_becomes_a_contact(db_session):
    """名字只留在公司欄位的話，聯絡人頁永遠是空的。"""
    repo = CompanyRepository(db_session)
    repo.upsert(
        _clean("測試公司", contact_person="王小明", email="ming@example.com",
               phone="02-27231234")
    )
    db_session.commit()

    contacts = ContactRepository(db_session).search()
    assert [c.name for c in contacts] == ["王小明"]
    assert contacts[0].email == "ming@example.com"
    assert contacts[0].is_primary is True


def test_recrawling_does_not_pile_up_the_same_person(db_session):
    repo = CompanyRepository(db_session)
    for _ in range(3):
        repo.upsert(_clean("測試公司", contact_person="王小明"))
        db_session.commit()

    assert ContactRepository(db_session).count() == 1


def test_a_second_person_is_added_without_stealing_primary(db_session):
    repo = CompanyRepository(db_session)
    repo.upsert(_clean("測試公司", contact_person="王小明"))
    db_session.commit()
    repo.upsert(_clean("測試公司", contact_person="陳大文"))
    db_session.commit()

    contacts = ContactRepository(db_session).search()
    assert sorted(c.name for c in contacts) == ["王小明", "陳大文"]
    assert [c.name for c in contacts if c.is_primary] == ["王小明"]


def test_no_contact_person_creates_nothing(db_session):
    repo = CompanyRepository(db_session)
    repo.upsert(_clean("測試公司"))
    repo.upsert(_clean("空白公司", dedupe_key="n:空白公司", contact_person="   "))
    db_session.commit()

    assert ContactRepository(db_session).count() == 0
