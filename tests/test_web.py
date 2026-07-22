from predict_mm.web import (
    MarketPayload,
    SetupPayload,
    _market_lookup_result,
    _market_locale_from_url,
    _market_slug_from_url,
    _markets_matching_slug,
    _search_query_from_slug,
    _validate_setup,
)


def test_market_slug_is_extracted_from_predict_url() -> None:
    assert _market_slug_from_url("https://predict.fun/market/btc-updown-15m-1783927800?tab=trade") == (
        "btc-updown-15m-1783927800"
    )


def test_market_slug_is_extracted_from_localized_predict_url() -> None:
    assert _market_slug_from_url("https://predict.fun/zh-cn/market/fifwc-fra-esp-2026-07-14") == (
        "fifwc-fra-esp-2026-07-14"
    )


def test_market_locale_is_extracted_from_localized_predict_url() -> None:
    assert _market_locale_from_url(
        "https://predict.fun/zh-cn/market/cxmt-ipo-closing-market-cap"
    ) == "zh-cn"
    assert _market_locale_from_url(
        "https://predict.fun/market/cxmt-ipo-closing-market-cap"
    ) == ""


def test_market_slug_rejects_non_predict_url() -> None:
    try:
        _market_slug_from_url("https://example.com/market/test")
    except ValueError as error:
        assert "predict.fun" in str(error)
    else:
        raise AssertionError("expected non-Predict URL to fail")


def test_search_query_removes_url_timestamp() -> None:
    assert _search_query_from_slug("btc-updown-15m-1783927800") == "btc updown 15m"


def test_market_url_results_are_filtered_to_the_exact_slug() -> None:
    slug = "dota2-vg-playti-2026-07-14"
    markets = [
        {"id": 1, "slug": "another-dota2-market"},
        {"id": 2, "categorySlug": slug, "title": "Match winner"},
        {"id": 3, "category": {"slug": slug}, "title": "Game 1 winner"},
        {"id": 4, "category": {"id": slug}, "title": "Total maps"},
        {"id": 5, "categorySlug": "dota2-vg-another-team-2026-07-14"},
    ]

    matches = _markets_matching_slug(markets, slug)

    assert [market["id"] for market in matches] == [2, 3, 4]


def test_market_url_filter_accepts_the_exact_market_slug() -> None:
    slug = "btc-updown-15m-1783927800"

    matches = _markets_matching_slug(
        [{"id": 42, "slug": slug}, {"id": 43, "slug": "btc-updown-15m-1783928700"}],
        slug,
    )

    assert [market["id"] for market in matches] == [42]


def test_market_lookup_exposes_all_outcomes_for_user_selection() -> None:
    result = _market_lookup_result(
        {
            "id": 42,
            "title": "Match winner",
            "categoryTitle": "France vs. Spain",
            "outcomes": [{"name": "FRA"}, {"name": "Draw"}, {"name": "ESP"}],
        }
    )

    assert result["outcomes"] == ["FRA", "Draw", "ESP"]
    assert result["category_title"] == "France vs. Spain"


def test_market_lookup_keeps_order_outcomes_canonical_on_chinese_pages() -> None:
    result = _market_lookup_result(
        {
            "id": 42,
            "question": "市值是否低于 1 万亿元？",
            "outcomes": [{"name": "是"}, {"name": "否"}],
        }
    )

    assert result["outcomes"] == ["Yes", "No"]


def test_setup_accepts_non_binary_market_outcome() -> None:
    payload = SetupPayload(
        markets=[
            MarketPayload(
                market_id="42",
                market_title="France vs Spain · Match winner",
                outcome="FRA",
            )
        ]
    )

    _validate_setup(payload)

    assert payload.markets[0].market_title == "France vs Spain · Match winner"


def test_setup_rejects_a_url_that_has_not_been_resolved() -> None:
    payload = SetupPayload(markets=[MarketPayload(market_id="https://predict.fun/market/example")])
    try:
        _validate_setup(payload)
    except Exception as error:  # HTTPException without requiring test helpers
        assert "识别网址" in str(error.detail)
    else:
        raise AssertionError("expected unresolved market URL to fail")


def test_setup_rejects_enabled_zero_run_duration() -> None:
    payload = SetupPayload(
        markets=[MarketPayload(market_id="42")],
        run_duration_enabled=True,
        run_duration_hours=0,
        run_duration_minutes=0,
    )

    try:
        _validate_setup(payload)
    except Exception as error:
        assert "不能同时为 0" in str(error.detail)
    else:
        raise AssertionError("expected zero run duration to fail")
