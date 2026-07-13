from predict_mm.web import MarketPayload, SetupPayload, _market_slug_from_url, _search_query_from_slug, _validate_setup


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


def test_setup_rejects_a_url_that_has_not_been_resolved() -> None:
    payload = SetupPayload(markets=[MarketPayload(market_id="https://predict.fun/market/example")])
    try:
        _validate_setup(payload)
    except Exception as error:  # HTTPException without requiring test helpers
        assert "识别网址" in str(error.detail)
    else:
        raise AssertionError("expected unresolved market URL to fail")
