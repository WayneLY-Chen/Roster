"""Tests for telling company records apart from promotional articles.

The asymmetry matters: leaving one ad in the database is an annoyance, but
dropping a real company is data loss the user never finds out about. So the
false-positive tests below (real names that must survive) carry more weight
than the detection tests, and several of them are the actual names harvested
from a live directory run.
"""

from __future__ import annotations

import pytest

from core.schemas import RawCompany
from verifier.classify import (
    classify,
    has_company_marker,
    is_article_url,
    is_probably_company,
    strip_decoration,
)
from verifier.service import CleaningService

# ------------------------------------------------- real companies must survive

REAL_COMPANIES = [
    "慶彬實業有限公司",
    "展杰工業有限公司",
    "孟益精密企業社",
    "旭澤工業股份有限公司",
    "東敏企業社(代工製造, 精密機械 ,塑膠加工,工程齒輪)",
    "宜峻工業有限公司-專業五軸加工",
    "力鵬 精密機械 有限公司",
    "宏達精密機械股份有限公司",
    "日新電子（股）公司",
    "祥發包裝材料行",
    "綠光紡織企業社",
    "海通物流股份有限公司",
    "台灣積體電路製造股份有限公司",
    "鴻海精密工業股份有限公司",
    "Acme Precision Co., Ltd.",
    "Sunrise Instrument Inc.",
]


@pytest.mark.parametrize("name", REAL_COMPANIES)
def test_real_company_names_are_kept(name: str) -> None:
    verdict = classify(name)
    assert verdict.is_company, f"{name} 被誤判為廣告：{verdict.reason}"


def test_long_comma_laden_company_name_survives() -> None:
    """Length plus commas must not outweigh a company suffix.

    Manufacturers routinely append their whole service list to the name in a
    directory listing, which trips both the length and the sentence-like
    heuristics -- the company marker has to win.
    """
    name = "東敏企業社(代工製造, 精密機械, 塑膠加工, 工程齒輪, CNC車床加工, 五金零件)"
    assert len(name) > 30
    assert is_probably_company(name)


def test_company_marker_beats_every_other_signal() -> None:
    """A company suffix wins even when other heuristics would reject."""
    assert is_probably_company(
        "⭐台中排風扇有限公司", "https://life.example.com/article/x"
    )


# -------------------------------------------------------------- advertisements

ADVERTS = [
    ("⭐台中排風扇設計全攻略!安裝排風扇怎麼放最涼?工班經驗談", None),
    # The real headline that slipped through: it contains 「工廠」, so treating
    # industry words as proof of companyhood let an article into the database.
    (
        "⭐台中排風扇設計全攻略!安裝排風扇怎麼放最涼?工廠、鐵皮屋排熱扇安裝還能享政府補助!",
        "https://life.iyp.com.tw/article/x",
    ),
    ("桃園實業廠房出租懶人包，租金行情一次看", None),
    ("台中沙鹿房價還有機會?雙港門戶+科技廊道崛起,解析未來增值潛力", None),
    ("🔧高雄電器維修服務懶人包｜冷氣、冰箱、洗衣機專業推薦", None),
    ("宜蘭打造安全又有質感的家，從專業門窗開始", "https://life.iyp.com.tw/article/宜蘭專業門窗"),
    ("2026最新推薦 十大工具機品牌評比", None),
    ("辦公室裝潢費用多少錢？一次看懂報價行情", None),
]


@pytest.mark.parametrize(("name", "website"), ADVERTS)
def test_advertisements_are_rejected(name: str, website: str | None) -> None:
    verdict = classify(name, website)
    assert not verdict.is_company
    assert verdict.reason


def test_empty_name_is_rejected() -> None:
    assert not classify("").is_company
    assert not classify(None).is_company


# -------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://life.iyp.com.tw/article/x", True),
        ("https://blog.example.com/", True),
        ("https://example.com/news/2026/story", True),
        ("https://example.com/promotion/spring", True),
        ("https://www.cb-cnc.com", False),
        ("https://www.shejer.com.tw/products", False),
        (None, False),
        ("", False),
    ],
)
def test_is_article_url(url: str | None, expected: bool) -> None:
    assert is_article_url(url) is expected


def test_strip_decoration_removes_leading_emoji() -> None:
    assert strip_decoration("⭐台中排風扇") == "台中排風扇"
    assert strip_decoration("🔧高雄電器維修") == "高雄電器維修"
    assert strip_decoration("慶彬實業有限公司") == "慶彬實業有限公司"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("慶彬實業有限公司", True),
        ("孟益精密企業社", True),
        ("Acme Co., Ltd.", True),
        ("台中沙鹿房價還有機會", False),
        # Industry words are not legal forms and must not count as proof.
        ("工廠、鐵皮屋排熱扇安裝", False),
        ("實業廠房出租", False),
    ],
)
def test_has_company_marker_only_matches_legal_forms(name: str, expected: bool) -> None:
    assert has_company_marker(name) is expected


# ------------------------------------------------- integration with cleaning


def _raw(name: str, website: str | None = None) -> RawCompany:
    return RawCompany(company_name=name, website=website, source="test")


def test_cleaning_service_drops_advertisements(patch_config) -> None:
    service = CleaningService(patch_config)

    assert service.clean(_raw("慶彬實業有限公司")) is not None
    assert service.clean(
        _raw("宜蘭打造安全又有質感的家，從專業門窗開始",
             "https://life.iyp.com.tw/article/x")
    ) is None


def test_cleaning_service_counts_advertisements_as_rejected(patch_config) -> None:
    service = CleaningService(patch_config)
    records = [
        _raw("慶彬實業有限公司"),
        _raw("展杰工業有限公司"),
        _raw("台中沙鹿房價還有機會?解析未來增值潛力"),
    ]

    cleaned, rejected = service.clean_many(records)

    assert len(cleaned) == 2
    assert rejected == 1


def test_filter_can_be_switched_off(patch_config) -> None:
    """Users who would rather review the noise themselves can opt out."""
    config = patch_config.model_copy(
        update={
            "verifier": patch_config.verifier.model_copy(
                update={"filter_advertisements": False}
            )
        }
    )
    service = CleaningService(config)

    kept = service.clean(
        _raw("台中沙鹿房價還有機會?解析未來增值潛力", "https://life.iyp.com.tw/article/x")
    )

    assert kept is not None
