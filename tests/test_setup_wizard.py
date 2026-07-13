from predict_mm.setup_wizard import MarketAnswers, WizardAnswers, build_config_text, build_env_text


def test_build_env_text_does_not_include_api_secret() -> None:
    text = build_env_text(
        WizardAnswers(
            api_key="api-key",
            jwt_token="jwt",
            private_key="private-key",
            market_id="1",
        )
    )

    assert "PREDICT_API_KEY=api-key" in text
    assert "PREDICT_JWT_TOKEN=jwt" in text
    assert "PREDICT_PRIVATE_KEY=private-key" in text
    assert "PREDICT_API_SECRET" not in text
    assert "PREDICT_CHAIN_ID" not in text


def test_build_config_text_defaults_to_dry_run() -> None:
    text = build_config_text(WizardAnswers(market_id="123", outcome="YES"))

    assert "dry_run = true" in text
    assert 'id = "123"' in text
    assert 'outcome = "YES"' in text
    assert 'quote_size = "1.0"' in text
    assert "cancel_after_seconds = 8" in text
    assert "is_neg_risk" not in text
    assert "is_yield_bearing" not in text
    assert "fee_rate_bps" not in text


def test_build_config_text_supports_multiple_markets() -> None:
    text = build_config_text(
        WizardAnswers(market_id="ignored", quote_size="2.0"),
        markets=[
            MarketAnswers(market_id="market-yes", outcome="YES", quote_size="1.0"),
            MarketAnswers(market_id="market-no", outcome="NO", quote_size="2.0"),
        ],
    )

    assert text.count("[[markets]]") == 2
    assert 'id = "market-yes"' in text
    assert 'id = "market-no"' in text
    assert 'quote_size = "2.0"' in text


def test_build_config_text_can_enable_emergency_exit() -> None:
    text = build_config_text(WizardAnswers(market_id="market", emergency_exit_on_buy_fill=True))

    assert "emergency_exit_on_buy_fill = true" in text
