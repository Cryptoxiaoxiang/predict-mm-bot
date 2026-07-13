from predict_mm.web import (
    MarketPayload,
    SetupPayload,
    _market_lookup_result,
    _market_slug_from_url,
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


def test_market_slug_rejects_non_predict_url() -> None:
    try:
        _market_slug_from_url("https://example.com/market/test")
    except ValueError as error:
        assert "predict.fun" in str(error)
    else:
        raise AssertionError("expected non-Predict URL to fail")


def test_search_query_removes_url_timestamp() -> None:
    assert _search_query_from_slug("btc-updown-15m-1783927800") == "btc updown 15m"


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


def test_setup_accepts_non_binary_market_outcome() -> None:
    _validate_setup(SetupPayload(markets=[MarketPayload(market_id="42", outcome="FRA")]))


def test_setup_rejects_a_url_that_has_not_been_resolved() -> None:
    payload = SetupPayload(markets=[MarketPayload(market_id="https://predict.fun/market/example")])
    try:
        _validate_setup(payload)
    except Exception as error:  # HTTPException without requiring test helpers
        assert "识别网址" in str(error.detail)
    else:
        raise AssertionError("expected unresolved market URL to fail")
