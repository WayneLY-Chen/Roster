"""Tests for core/config.py."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import core.config as config_module
from core.constants import DISPLAY_NAME
from core.config import (
    AppConfig,
    CrawlerSection,
    PaginationRule,
    SourceConfig,
    load_config,
)
from core.errors import ConfigError


@pytest.fixture(autouse=True)
def _reset_config_cache():
    """Every test in this module manages ``load_config``/``get_config`` itself;
    make sure none of that leaks into other test modules."""
    config_module.reset_config()
    yield
    config_module.reset_config()


# --------------------------------------------------------------- load_config


def test_load_config_reads_valid_yaml(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "app": {"name": "Custom CRM", "theme": "dark"},
                "database": {"url": "sqlite:///./custom.db"},
                "crawler": {"delay_seconds": 5.0},
            }
        ),
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.app.name == "Custom CRM"
    assert config.app.theme == "dark"
    assert config.crawler.delay_seconds == 5.0


def test_load_config_defaults_when_default_path_is_absent(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", tmp_path / "missing.yaml")
    config = load_config()
    assert isinstance(config, AppConfig)
    # 設定檔不見時要退回程式碼裡的預設值，而那個預設值必須是這支程式現在
    # 的名字——不是改名前的舊名。名稱會出現在視窗標題上。
    assert config.app.name == DISPLAY_NAME


def test_load_config_explicit_missing_path_raises(tmp_path: Path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "does-not-exist.yaml")


def test_load_config_malformed_yaml_raises_config_error(tmp_path: Path):
    path = tmp_path / "bad.yaml"
    path.write_text("app: [unterminated\n  nested: -", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_load_config_non_mapping_yaml_raises_config_error(tmp_path: Path):
    path = tmp_path / "list.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_load_config_empty_file_uses_defaults(tmp_path: Path):
    path = tmp_path / "empty.yaml"
    path.write_text("", encoding="utf-8")
    config = load_config(path)
    assert isinstance(config, AppConfig)


def test_load_config_invalid_values_raise_config_error(tmp_path: Path):
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump({"app": {"theme": "neon"}}), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_get_config_is_cached_until_reset(monkeypatch, tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"app": {"name": "First"}}), encoding="utf-8")
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", path)

    first = config_module.get_config()
    assert first.app.name == "First"

    path.write_text(yaml.safe_dump({"app": {"name": "Second"}}), encoding="utf-8")
    still_cached = config_module.get_config()
    assert still_cached is first
    assert still_cached.app.name == "First"

    config_module.reset_config()
    refreshed = config_module.get_config()
    assert refreshed.app.name == "Second"


# --------------------------------------------------------------- SourceConfig


def test_source_config_generic_html_requires_start_url():
    with pytest.raises(ValidationError, match="start_url is required"):
        SourceConfig(name="x", type="generic_html")


def test_source_config_generic_html_requires_list_selector():
    with pytest.raises(ValidationError, match="list_selector is required"):
        SourceConfig(name="x", type="generic_html", start_url="https://example.com/{page}")


def test_source_config_generic_html_requires_company_name_field():
    with pytest.raises(ValidationError, match="fields.company_name is required"):
        SourceConfig(
            name="x",
            type="generic_html",
            start_url="https://example.com/{page}",
            list_selector=".item",
        )


def test_source_config_query_pagination_requires_page_placeholder():
    with pytest.raises(ValidationError, match=r"must contain '\{page\}'"):
        SourceConfig(
            name="x",
            type="generic_html",
            start_url="https://example.com/list",
            list_selector=".item",
            fields={"company_name": {"selector": ".name"}},
            pagination={"type": "query"},
        )


def test_source_config_next_link_pagination_requires_next_selector():
    with pytest.raises(ValidationError):
        PaginationRule(type="next_link")


def test_source_config_valid_generic_html_source():
    source = SourceConfig(
        name="x",
        type="generic_html",
        start_url="https://example.com/list?page={page}",
        list_selector=".item",
        fields={"company_name": {"selector": ".name"}},
        pagination={"type": "query"},
    )
    assert source.source_label == "x"


def test_source_config_method_and_encoding_default_to_get_and_none():
    """既有的 source 設定不帶 method/form_data/encoding 也要能正常驗證通過。"""
    source = SourceConfig(
        name="x",
        type="generic_html",
        start_url="https://example.com/{page}",
        list_selector=".item",
        fields={"company_name": {"selector": ".name"}},
    )
    assert source.method == "GET"
    assert source.form_data is None
    assert source.encoding is None


def test_source_config_query_pagination_allows_page_placeholder_in_form_data():
    """POST 表單分頁時，{page} 可以只出現在 form_data 裡，不必出現在網址上。"""
    source = SourceConfig(
        name="tca",
        type="generic_html",
        start_url="https://example.com/search",  # 沒有 {page}
        list_selector=".item",
        fields={"company_name": {"selector": ".name"}},
        pagination={"type": "query"},
        method="POST",
        form_data={"keyword": "", "page": "{page}"},
    )
    assert source.method == "POST"
    assert source.form_data == {"keyword": "", "page": "{page}"}


def test_source_config_form_data_requires_post_method():
    with pytest.raises(ValidationError, match="form_data is only meaningful"):
        SourceConfig(
            name="x",
            type="generic_html",
            start_url="https://example.com/{page}",
            list_selector=".item",
            fields={"company_name": {"selector": ".name"}},
            form_data={"q": "1"},
        )


def test_source_config_encoding_option_is_stored_verbatim():
    source = SourceConfig(
        name="tca",
        type="generic_html",
        start_url="https://example.com/{page}",
        list_selector=".item",
        fields={"company_name": {"selector": ".name"}},
        encoding="big5",
    )
    assert source.encoding == "big5"


def test_source_config_sample_type_needs_nothing_extra():
    source = SourceConfig(name="sample", type="sample")
    assert source.source_label == "sample"


def test_source_config_label_overrides_name_for_source_label():
    source = SourceConfig(name="internal", type="sample", label="Pretty Name")
    assert source.source_label == "Pretty Name"


def test_crawler_section_rejects_duplicate_source_names():
    with pytest.raises(ValidationError, match="duplicate crawler source name"):
        CrawlerSection(
            sources=[
                {"name": "dup", "type": "sample"},
                {"name": "dup", "type": "sample"},
            ]
        )


def test_crawler_section_source_lookup_and_enabled_sources():
    section = CrawlerSection(
        sources=[
            {"name": "a", "type": "sample", "enabled": True},
            {"name": "b", "type": "sample", "enabled": False},
        ]
    )
    assert section.source("a").name == "a"
    assert [s.name for s in section.enabled_sources()] == ["a"]
    with pytest.raises(ConfigError):
        section.source("does-not-exist")


# ---------------------------------------------------------- resolved_user_agent


def test_resolved_user_agent_substitutes_contact(monkeypatch):
    monkeypatch.setenv("CRM_CRAWLER_CONTACT", "me@example.com")
    section = CrawlerSection(user_agent="Bot/1.0 (+{contact})")
    assert section.resolved_user_agent() == "Bot/1.0 (+me@example.com)"


def test_resolved_user_agent_falls_back_when_env_unset(monkeypatch):
    monkeypatch.delenv("CRM_CRAWLER_CONTACT", raising=False)
    section = CrawlerSection(user_agent="Bot/1.0 (+{contact})")
    assert section.resolved_user_agent() == "Bot/1.0 (+unset@example.com)"


# --------------------------------------------------------------- path helpers


def test_database_section_sqlite_path_and_resolved_url(tmp_path: Path):
    from core.config import DatabaseSection

    section = DatabaseSection(url=f"sqlite:///{(tmp_path / 'sub' / 'crm.db').as_posix()}")
    assert section.sqlite_path == (tmp_path / "sub" / "crm.db").resolve()
    assert section.resolved_url == f"sqlite:///{(tmp_path / 'sub' / 'crm.db').resolve().as_posix()}"


def test_database_section_sqlite_path_none_for_memory_and_other_backends():
    from core.config import DatabaseSection

    assert DatabaseSection(url="sqlite:///:memory:").sqlite_path is None
    assert DatabaseSection(url="postgresql://localhost/db").sqlite_path is None
    assert DatabaseSection(url="postgresql://localhost/db").resolved_url == (
        "postgresql://localhost/db"
    )


def test_logging_exporter_backup_resolved_dirs_are_absolute():
    config = AppConfig()
    assert config.logging.resolved_dir.is_absolute()
    assert config.exporter.resolved_output_dir.is_absolute()
    assert config.backup.resolved_dir.is_absolute()


def test_ensure_directories_creates_everything(tmp_path: Path):
    config = AppConfig.model_validate(
        {
            "database": {"url": f"sqlite:///{(tmp_path / 'data' / 'crm.db').as_posix()}"},
            "logging": {"dir": str(tmp_path / "logs")},
            "exporter": {"output_dir": str(tmp_path / "output")},
            "backup": {"dir": str(tmp_path / "backups")},
        }
    )
    config.ensure_directories()
    assert (tmp_path / "data").is_dir()
    assert (tmp_path / "logs").is_dir()
    assert (tmp_path / "output").is_dir()
    assert (tmp_path / "backups").is_dir()


# ------------------------------------------------------------------- frozen


def test_app_config_is_frozen_and_rejects_unknown_fields():
    config = AppConfig()
    with pytest.raises(ValidationError):
        config.app = config.app  # frozen models forbid attribute assignment
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"unknown_top_level_field": 1})


# ------------------------------------------------------------ custom sources
#
# ``CUSTOM_SOURCES_PATH`` is a module-level constant (not something an
# ``AppConfig`` carries), so every test here redirects it into ``tmp_path``
# first -- otherwise saving a source would write into the real project root.


@pytest.fixture(autouse=True)
def _custom_sources_path(monkeypatch, tmp_path):
    path = tmp_path / "custom_sources.yaml"
    monkeypatch.setattr(config_module, "CUSTOM_SOURCES_PATH", path)
    return path


def test_read_custom_sources_missing_file_returns_empty_list():
    assert config_module.read_custom_sources() == []


def test_read_custom_sources_malformed_yaml_raises_config_error(_custom_sources_path):
    _custom_sources_path.write_text("sources: [unterminated", encoding="utf-8")
    with pytest.raises(ConfigError):
        config_module.read_custom_sources()


def test_read_custom_sources_non_list_raises_config_error(_custom_sources_path):
    _custom_sources_path.write_text(
        yaml.safe_dump({"sources": "not-a-list"}), encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        config_module.read_custom_sources()


def test_read_custom_sources_filters_invalid_entries(_custom_sources_path):
    _custom_sources_path.write_text(
        yaml.safe_dump({"sources": [{"name": "good", "type": "sample"}, {"type": "sample"}, "junk"]}),
        encoding="utf-8",
    )
    entries = config_module.read_custom_sources()
    assert entries == [{"name": "good", "type": "sample"}]


def test_save_custom_source_writes_and_replaces_by_name():
    from core.config import SourceConfig

    first = SourceConfig(name="mine", type="sample")
    config_module.save_custom_source(first)
    assert [s["name"] for s in config_module.read_custom_sources()] == ["mine"]

    second = SourceConfig(name="mine", type="sample", label="Renamed")
    config_module.save_custom_source(second)
    entries = config_module.read_custom_sources()
    assert len(entries) == 1
    assert entries[0]["label"] == "Renamed"


def test_delete_custom_source_returns_false_when_absent():
    assert config_module.delete_custom_source("nope") is False


def test_delete_custom_source_removes_a_saved_source():
    from core.config import SourceConfig

    config_module.save_custom_source(SourceConfig(name="mine", type="sample"))
    assert config_module.delete_custom_source("mine") is True
    assert config_module.read_custom_sources() == []


def test_load_config_merges_custom_sources_over_config_yaml(tmp_path, _custom_sources_path):
    _custom_sources_path.write_text(
        yaml.safe_dump({"sources": [{"name": "sample", "type": "sample", "label": "Overridden"}]}),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "crawler": {
                    "sources": [
                        {"name": "sample", "type": "sample"},
                        {"name": "other", "type": "sample"},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    config = load_config(config_path)
    names = [s.name for s in config.crawler.sources]
    assert names == ["other", "sample"]  # replaced source moves to the end
    assert config.crawler.source("sample").source_label == "Overridden"


def test_load_config_merges_custom_sources_when_config_yaml_has_no_crawler_section(
    tmp_path, _custom_sources_path
):
    _custom_sources_path.write_text(
        yaml.safe_dump({"sources": [{"name": "mine", "type": "sample"}]}), encoding="utf-8"
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"app": {"name": "X"}}), encoding="utf-8")

    config = load_config(config_path)
    assert [s.name for s in config.crawler.sources] == ["mine"]


def test_load_config_custom_sources_reject_non_mapping_crawler_section(
    tmp_path, _custom_sources_path
):
    _custom_sources_path.write_text(
        yaml.safe_dump({"sources": [{"name": "mine", "type": "sample"}]}), encoding="utf-8"
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"crawler": "oops"}), encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_load_config_custom_sources_reject_non_list_sources(tmp_path, _custom_sources_path):
    _custom_sources_path.write_text(
        yaml.safe_dump({"sources": [{"name": "mine", "type": "sample"}]}), encoding="utf-8"
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"crawler": {"sources": "oops"}}), encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(config_path)


# ------------------------------------------------------- mailer / scheduler


def test_mailer_section_defaults():
    from core.config import MailerSection

    section = MailerSection()
    assert section.dry_run is True
    assert section.enabled is False
    assert section.resolved_templates_dir.is_absolute()


def test_mailer_section_credentials_delegate_to_the_credential_vault(monkeypatch):
    """``MailerSection.address``/``app_password`` are one-line delegations to
    ``core.credentials.get_secret``; that module's own storage/env/keyring
    fallback logic is out of this suite's scope, so only the delegation
    itself is pinned here."""
    import core.credentials as credentials_module
    from core.config import MailerSection

    monkeypatch.setattr(
        credentials_module, "get_secret", lambda name: f"secret-for-{name}"
    )
    section = MailerSection()
    assert section.address == "secret-for-gmail_address"
    assert section.app_password == "secret-for-gmail_app_password"


def test_scheduler_section_valid_time_of_day():
    from core.config import SchedulerSection

    section = SchedulerSection(at="23:59")
    assert section.at == "23:59"


@pytest.mark.parametrize("bad_time", ["25:00", "12:60", "not-a-time", "12"])
def test_scheduler_section_rejects_invalid_time_of_day(bad_time):
    from core.config import SchedulerSection

    with pytest.raises(ValidationError):
        SchedulerSection(at=bad_time)


# --------------------------------------------------------------- 使用者設定

# GUI 上的開關存在 user_settings.yaml，不寫回 config.yaml——後者的註解就是它大半
# 的價值，程式化地重寫會把註解全部清掉。


@pytest.fixture
def user_settings_file(tmp_path, monkeypatch):
    """把 user_settings.yaml 導到 tmp_path，別碰到真正的專案資料夾。"""
    import core.config as config_module

    target = tmp_path / "user_settings.yaml"
    monkeypatch.setattr(config_module, "USER_SETTINGS_PATH", target)
    yield target
    config_module.reset_config()


def test_no_user_settings_file_reads_as_empty(user_settings_file):
    from core.config import read_user_settings

    assert read_user_settings() == {}


def test_saved_setting_is_read_back(user_settings_file):
    from core.config import read_user_settings, save_user_setting

    save_user_setting("mailer", "dry_run", False)
    assert read_user_settings() == {"mailer": {"dry_run": False}}


def test_saving_one_key_keeps_the_others(user_settings_file):
    """一次只該動一個鍵，不能把隔壁的設定重設回預設值。"""
    from core.config import read_user_settings, save_user_setting

    save_user_setting("mailer", "enabled", True)
    save_user_setting("mailer", "dry_run", False)
    assert read_user_settings()["mailer"] == {"enabled": True, "dry_run": False}


def test_user_settings_override_config_yaml(user_settings_file, tmp_path):
    from core.config import load_config, save_user_setting

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "mailer:\n  enabled: false\n  daily_limit: 42\n", encoding="utf-8"
    )

    assert load_config(config_file).mailer.enabled is False

    save_user_setting("mailer", "enabled", True)
    merged = load_config(config_file)
    assert merged.mailer.enabled is True
    assert merged.mailer.daily_limit == 42, "覆寫一個鍵不該蓋掉整個區段"


def test_an_invalid_setting_is_rolled_back(user_settings_file):
    """存下去會讓程式開不起來的值，要當場失敗並還原，而不是留到下次啟動。"""
    from core.config import read_user_settings, save_user_setting
    from core.errors import ConfigError

    save_user_setting("mailer", "enabled", True)

    with pytest.raises(ConfigError):
        save_user_setting("mailer", "daily_limit", -5)

    assert read_user_settings()["mailer"] == {"enabled": True}


def test_broken_user_settings_file_is_reported(user_settings_file):
    from core.config import read_user_settings
    from core.errors import ConfigError

    user_settings_file.write_text("mailer: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        read_user_settings()


def test_user_settings_must_be_a_mapping(user_settings_file):
    from core.config import read_user_settings
    from core.errors import ConfigError

    user_settings_file.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        read_user_settings()
