from predict_mm.setup_wizard import WizardAnswers, build_config_text, build_env_text


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


def test_build_config_text_defaults_to_dry_run() -> None:
    text = build_config_text(WizardAnswers(market_id="123", outcome="YES"))

    assert "dry_run = true" in text
    assert 'id = "123"' in text
    assert 'outcome = "YES"' in text
    assert 'quote_size = "1.0"' in text
    assert "cancel_after_seconds = 8" in text
